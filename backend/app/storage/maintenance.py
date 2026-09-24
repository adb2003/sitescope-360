"""Collecting the parts of uploads nobody finished (§24).

A chunked upload that is neither completed nor aborted leaves its parts behind. On S3 that is a
solved problem — an `AbortIncompleteMultipartUpload` lifecycle rule on the bucket removes them —
but the filesystem backend, which is the default and the one this project is built to run on
without any cloud account, has no equivalent. `LocalStorage.purge_stale_uploads` was written as
the substitute and then never called, so in practice every abandoned upload left its parts under
`staging/` permanently: a 2 GB walkthrough failing at ninety percent strands nearly 2 GB.

Two triggers, and no scheduler between them:

* **At startup**, because a deploy is a natural moment to sweep and costs nothing.
* **When a new chunked upload begins**, throttled to once an hour per process. Tying the cleanup
  to the activity that produces the garbage means a busy deployment sweeps often and an idle one
  does not spin — and it needs no cron, no beat worker and no second process, which is the whole
  reason this application can run on a laptop.

It never raises and never blocks the upload it was triggered by. Failing to collect old parts is
a disk-space problem; failing the request the operator is making is their problem, and the wrong
one to create while tidying up.
"""

from __future__ import annotations

import threading
import time

from app.core.logging import get_logger, log_event
from app.storage import get_storage

logger = get_logger(__name__)

#: How often the opportunistic sweep may actually run, per process.
#:
#: An hour, because the thing being collected is a day old by default — sweeping more often finds
#: nothing, and sweeping less often lets a deployment that never restarts accumulate indefinitely.
PURGE_INTERVAL_SECONDS = 3600

_last_purge = 0.0
_lock = threading.Lock()


def purge_abandoned_uploads(*, force: bool = False) -> int:
    """Delete parts from chunked uploads nobody finished, and say how many uploads were dropped.

    ``force`` bypasses the throttle, for the startup sweep. Returns 0 rather than raising on any
    backend without the capability: S3 has the bucket rule, and a provider added later should not
    have to implement a cleanup it does not need.
    """
    storage = get_storage()
    purge = getattr(storage, "purge_stale_uploads", None)
    if purge is None:
        return 0

    now = time.monotonic()
    with _lock:
        # Checked and stamped under the same lock, so two requests arriving together produce one
        # sweep rather than two walking the same directory.
        if not force and now - _last_purge < PURGE_INTERVAL_SECONDS:
            return 0
        _set_last_purge(now)

    try:
        dropped = int(purge())
    except Exception:
        logger.warning("Could not sweep abandoned uploads; parts may be left in staging")
        return 0

    if dropped:
        log_event("storage.stale_uploads_purged", uploads=dropped)
    return dropped


def _set_last_purge(value: float) -> None:
    global _last_purge
    _last_purge = value


def reset_purge_throttle() -> None:
    """Forget when the last sweep happened. For tests."""
    _set_last_purge(0.0)
