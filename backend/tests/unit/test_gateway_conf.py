"""The gateway sets security headers for what it serves, and only for that (§44).

`nginx.conf` is not executed by this suite — there is no nginx and no Docker in CI — so this is
the same bargain `test_compose.py` makes: parse the file structurally and assert the few
properties that were got wrong, rather than pretend it has been run.

The property that matters is **one owner per response**. These headers used to be set once at
`server` level, which looked tidier and was wrong twice over: a deployment that does not run this
gateway got nothing at all, and the SPA's `script-src 'self'` was applied to the two
documentation pages the API serves itself, which load their viewer from a CDN — so the production
stack served a blank `/docs`.

Setting them in both places is not the fix either, and that is the subtle half. Two
`Content-Security-Policy` headers are both enforced and the browser applies the *intersection*,
so a second policy at this layer would re-break that page whatever the application sent. Hence:
the proxied locations carry no policy, and the locations nginx answers itself carry one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

CONF_PATH = Path(__file__).resolve().parents[3] / "infrastructure" / "nginx" / "nginx.conf"

#: What the SPA — including the public share page at `/s/{token}` — must be served with.
REQUIRED_SPA_HEADERS = {
    "X-Content-Type-Options",
    "X-Frame-Options",
    "Referrer-Policy",
    "Permissions-Policy",
    "Content-Security-Policy",
}


def _strip_comments(text: str) -> str:
    return "\n".join(line.split("#", 1)[0] for line in text.splitlines())


@pytest.fixture(scope="module")
def conf() -> str:
    assert CONF_PATH.is_file(), f"gateway config not found at {CONF_PATH}"
    return _strip_comments(CONF_PATH.read_text(encoding="utf-8"))


def test_the_braces_balance(conf: str) -> None:
    """The cheapest syntax check available without nginx, and it has caught worse files."""
    depth = 0
    for char in conf:
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            assert depth >= 0, "a closing brace with nothing open"
    assert depth == 0, f"{depth} block(s) left unclosed"


def _blocks(conf: str) -> dict[str, str]:
    """Each `location ...` block's body, keyed by the text of its header line."""
    blocks: dict[str, str] = {}
    index = 0
    while (start := conf.find("location", index)) != -1:
        open_brace = conf.find("{", start)
        if open_brace == -1:
            break
        name = " ".join(conf[start:open_brace].split())
        depth, cursor = 1, open_brace + 1
        while depth and cursor < len(conf):
            depth += {"{": 1, "}": -1}.get(conf[cursor], 0)
            cursor += 1
        blocks[name] = conf[open_brace + 1 : cursor - 1]
        index = cursor
    return blocks


def test_the_spa_location_sets_every_header(conf: str) -> None:
    """`location /` is the one the gateway answers for itself, share page included."""
    spa = _blocks(conf)["location /"]

    for header in REQUIRED_SPA_HEADERS:
        assert f'add_header {header} "' in spa, f"the SPA location does not set {header}"
    # The share page is reached through this location, so this is what answers "can any site
    # frame the shared viewer".
    assert "frame-ancestors 'self'" in spa


def test_no_header_is_set_outside_a_location(conf: str) -> None:
    """The regression test for the shape, not just the values.

    A server-level `add_header` would apply to the proxied API and documentation responses too,
    which is how the duplicate-CSP intersection comes back.
    """
    total = conf.count("add_header")
    accounted = sum(body.count("add_header") for body in _blocks(conf).values())

    assert total > 0, "the gateway sets no headers at all"
    assert total == accounted, (
        f"{total - accounted} add_header directive(s) sit outside any location block"
    )


def test_no_proxied_api_location_sets_a_policy(conf: str) -> None:
    """One owner per response: the application sets these for everything it serves."""
    for name, body in _blocks(conf).items():
        if "sitescope_api" not in body:
            continue
        assert "Content-Security-Policy" not in body, (
            f"{name} proxies to the API and also sets a CSP — the browser would enforce both"
        )


def test_the_error_page_nginx_writes_itself_carries_them(conf: str) -> None:
    """The 429 body is generated here, so nothing downstream can add the headers to it."""
    limited = _blocks(conf)["location = /__rate_limited"]

    assert 'add_header X-Content-Type-Options "nosniff"' in limited
    assert "frame-ancestors 'none'" in limited
