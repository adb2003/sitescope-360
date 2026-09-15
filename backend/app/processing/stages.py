"""Pipeline stage implementations (§8).

Every stage obeys the same contract:

* takes a database session, the capture session, and the job spec;
* is **idempotent** — re-running it replaces its own output rather than duplicating it;
* returns a JSON-serialisable dict recorded on the ``ProcessingJob`` row;
* raises :class:`ProcessingError` with a human-readable message on failure.

That contract is what makes per-stage retry safe, and it is why the stages know nothing about the
queue: the same functions run under RQ, under the thread pool, and directly in tests.
"""

from __future__ import annotations

import contextlib
import tempfile
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import numpy as np
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.enums import (
    DataProvenance,
    FileKind,
    JobType,
    MappingConfidence,
    ProjectionType,
    QualityVerdict,
)
from app.core.errors import ProcessingError
from app.core.ids import new_id
from app.core.logging import get_logger, log_event
from app.models import (
    CaptureFile,
    CaptureFrame,
    CapturePath,
    CaptureSession,
    QualityResult,
)
from app.processing import media
from app.queue.base import JobSpec
from app.storage import get_storage, session_key
from app.storage.base import StorageProvider
from app.vision.engine import get_vision_engine
from app.vision.features import deserialize_features, serialize_features
from app.vision.fisheye import looks_like_dual_fisheye, stitch_to_equirectangular
from app.vision.panorama import hamming_distance
from app.vision.types import FeatureSet

logger = get_logger(__name__)

#: Frames closer than this in dHash space are treated as the same viewpoint.
DUPLICATE_HAMMING = 6
#: Feature detection and matching on hundreds of full-resolution panoramas is the most expensive
#: part of the pipeline. Matching quality saturates well below native resolution, so frames are
#: analysed at this width — a ~10x saving on an 8k panorama with no measurable accuracy loss.
MAPPING_ANALYSIS_WIDTH = 2048


# ---- helpers -----------------------------------------------------------------
@contextlib.contextmanager
def _staged_file(storage: StorageProvider, key: str, suffix: str) -> Iterator[Path]:
    """Give OpenCV a real path, whatever the storage backend is."""
    direct = storage.local_path(key)
    if direct:
        yield Path(direct)
        return
    # Not a context manager on purpose: the handle must be closed before OpenCV can open the
    # path on Windows, and the file must outlive that close until the caller is done with it.
    handle = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)  # noqa: SIM115
    try:
        handle.write(storage.read_bytes(key))
        handle.flush()
        handle.close()
        yield Path(handle.name)
    finally:
        Path(handle.name).unlink(missing_ok=True)


def _files_for(db: Session, session: CaptureSession, spec: JobSpec) -> list[CaptureFile]:
    """The files a stage should act on: one, if the spec names it, else all of them."""
    if spec.file_id:
        found = db.get(CaptureFile, spec.file_id)
        return [found] if found else []
    return list(
        db.scalars(
            select(CaptureFile)
            .where(CaptureFile.session_id == session.id)
            .order_by(CaptureFile.created_at, CaptureFile.original_filename)
        )
    )


# ---- 1. VALIDATE -------------------------------------------------------------
def stage_validate(db: Session, session: CaptureSession, spec: JobSpec) -> dict[str, Any]:
    """Confirm each file is what it claims, and classify its projection (§7)."""
    storage = get_storage()
    engine = get_vision_engine()
    files = _files_for(db, session, spec)
    if not files:
        raise ProcessingError("Capture session has no files to process")

    summary: list[dict[str, Any]] = []
    for capture_file in files:
        errors: list[str] = []
        warnings: list[str] = []

        if not storage.exists(capture_file.storage_key):
            raise ProcessingError(f"Uploaded object is missing: {capture_file.original_filename}")

        size = storage.size(capture_file.storage_key)
        capture_file.file_size_bytes = size
        if size == 0:
            errors.append("File is empty")
        if size > settings.max_upload_bytes:
            errors.append(f"File exceeds the {settings.max_upload_bytes} byte limit")

        with storage.open(capture_file.storage_key) as stream:
            header = stream.read(64)
        sniffed = media.sniff(header, capture_file.original_filename)
        capture_file.content_type = sniffed.content_type

        if sniffed.is_video:
            _validate_video(storage, capture_file, errors, warnings)
        else:
            _validate_image(storage, engine, capture_file, errors, warnings)

        capture_file.validation_errors = errors or None
        capture_file.validation_warnings = warnings or None
        summary.append(
            {
                "file_id": capture_file.id,
                "filename": capture_file.original_filename,
                "kind": str(capture_file.kind),
                "projection": str(capture_file.projection),
                "width": capture_file.width_px,
                "height": capture_file.height_px,
                "errors": errors,
                "warnings": warnings,
            }
        )

    db.flush()
    failed = [item for item in summary if item["errors"]]
    if len(failed) == len(summary):
        raise ProcessingError(
            "Every uploaded file failed validation",
            files=[{"filename": f["filename"], "errors": f["errors"]} for f in failed],
        )
    return {"files": summary, "failed_count": len(failed)}


def _validate_image(
    storage: StorageProvider,
    engine: Any,
    capture_file: CaptureFile,
    errors: list[str],
    warnings: list[str],
) -> None:
    data = storage.read_bytes(capture_file.storage_key)
    try:
        image = media.decode_image(data)
    except ProcessingError as exc:
        errors.append(str(exc))
        capture_file.kind = FileKind.UNKNOWN
        return

    metadata = media.extract_image_metadata(data)
    info = engine.describe(image, xmp_projection=metadata.xmp_projection)

    capture_file.width_px = info.width
    capture_file.height_px = info.height
    capture_file.projection = info.projection

    # Checked before the equirectangular branch, because a dual-fisheye frame is also 2:1 and the
    # describe() heuristic has every reason to call it equirectangular. Treating it as one does
    # not degrade the capture, it makes it meaningless: the pixels do not mean what the feature
    # detector, the motion estimator and the viewer all read them as.
    if looks_like_dual_fisheye(image):
        capture_file.projection = ProjectionType.DUAL_FISHEYE
        capture_file.kind = FileKind.PANORAMA
        warnings.append(
            "This looks like a raw dual-fisheye recording. Frames will be stitched to "
            "equirectangular during processing; the stitch uses assumed lens geometry, so "
            "check the seam before relying on positions from it."
        )
    elif info.projection is ProjectionType.EQUIRECTANGULAR:
        capture_file.kind = FileKind.PANORAMA
    elif info.projection is ProjectionType.PERSPECTIVE:
        capture_file.kind = FileKind.PERSPECTIVE_IMAGE
        warnings.append(
            "This looks like a normal perspective photo, not a 360 panorama. It will be stored "
            "and viewable, but cannot be navigated as a 360 scene."
        )
    else:
        # Ambiguous geometry is not a rejection reason (§7) — record the uncertainty and keep it.
        capture_file.kind = FileKind.PANORAMA
        warnings.append(
            f"Projection is ambiguous (confidence {info.equirect_confidence:.0%}); treating it "
            "as equirectangular. Evidence: " + "; ".join(info.reasons or ["none"])
        )

    if info.width < 1024:
        warnings.append(f"Low resolution: {info.width}×{info.height}")


def _validate_video(
    storage: StorageProvider,
    capture_file: CaptureFile,
    errors: list[str],
    warnings: list[str],
) -> None:
    suffix = Path(capture_file.original_filename).suffix or ".mp4"
    with _staged_file(storage, capture_file.storage_key, suffix) as path:
        try:
            metadata = media.extract_video_metadata(path)
        except ProcessingError as exc:
            errors.append(str(exc))
            capture_file.kind = FileKind.UNKNOWN
            return

    capture_file.width_px = metadata.width
    capture_file.height_px = metadata.height
    capture_file.duration_seconds = metadata.duration_seconds
    capture_file.fps = metadata.fps

    aspect = metadata.width / metadata.height if metadata.width and metadata.height else 0.0
    if 1.75 <= aspect <= 2.25:
        capture_file.kind = FileKind.VIDEO_360
        capture_file.projection = ProjectionType.EQUIRECTANGULAR
    else:
        capture_file.kind = FileKind.VIDEO_FLAT
        capture_file.projection = ProjectionType.PERSPECTIVE
        warnings.append(
            f"Video aspect ratio {aspect:.2f} is not 2:1, so this is probably not an "
            "equirectangular 360 video. Frames will be extracted as flat images."
        )

    if not metadata.duration_seconds:
        warnings.append("Video duration is unknown; frame extraction will read to end of file.")


# ---- 2. METADATA -------------------------------------------------------------
def stage_metadata(db: Session, session: CaptureSession, spec: JobSpec) -> dict[str, Any]:
    """EXIF, GPS, camera identity, video timing (§7)."""
    storage = get_storage()
    files = _files_for(db, session, spec)
    extracted: list[dict[str, Any]] = []

    for capture_file in files:
        if capture_file.kind in (FileKind.VIDEO_360, FileKind.VIDEO_FLAT):
            suffix = Path(capture_file.original_filename).suffix or ".mp4"
            with _staged_file(storage, capture_file.storage_key, suffix) as path:
                metadata = media.extract_video_metadata(path)
        else:
            metadata = media.extract_image_metadata(storage.read_bytes(capture_file.storage_key))

        capture_file.camera_make = metadata.camera_make or capture_file.camera_make
        capture_file.camera_model = metadata.camera_model or capture_file.camera_model
        capture_file.captured_at = metadata.captured_at or capture_file.captured_at
        capture_file.gps_latitude = (
            metadata.gps_latitude
            if metadata.gps_latitude is not None
            else capture_file.gps_latitude
        )
        capture_file.gps_longitude = (
            metadata.gps_longitude
            if metadata.gps_longitude is not None
            else capture_file.gps_longitude
        )
        capture_file.gps_altitude_m = (
            metadata.gps_altitude_m
            if metadata.gps_altitude_m is not None
            else capture_file.gps_altitude_m
        )
        if metadata.duration_seconds:
            capture_file.duration_seconds = metadata.duration_seconds
        if metadata.fps:
            capture_file.fps = metadata.fps
        capture_file.metadata_json = metadata.raw or None

        # Adopt the camera name from the imagery if the operator did not pick a device.
        if not session.camera_label and (metadata.camera_make or metadata.camera_model):
            session.camera_label = " ".join(
                part for part in (metadata.camera_make, metadata.camera_model) if part
            )
        # A GPS fix from the first file is better than none for the session.
        if session.gps_latitude is None and metadata.gps_latitude is not None:
            session.gps_latitude = metadata.gps_latitude
            session.gps_longitude = metadata.gps_longitude

        extracted.append(
            {
                "file_id": capture_file.id,
                "camera": session.camera_label,
                "captured_at": capture_file.captured_at.isoformat()
                if capture_file.captured_at
                else None,
                "has_gps": metadata.gps_latitude is not None,
                "duration_seconds": capture_file.duration_seconds,
                "fps": capture_file.fps,
            }
        )

    db.flush()
    return {"files": extracted}


# ---- 3. FRAME_EXTRACTION -----------------------------------------------------
def stage_frame_extraction(db: Session, session: CaptureSession, spec: JobSpec) -> dict[str, Any]:
    """Stills become one frame each; videos become one frame every N seconds (§9)."""
    storage = get_storage()
    files = _files_for(db, session, spec)
    interval = float(
        spec.payload.get("frame_interval_seconds")
        or session.frame_interval_seconds
        or settings.frame_interval_seconds
    )

    # Idempotency: drop this stage's previous output before regenerating.
    for capture_file in files:
        _delete_frames_for_file(db, capture_file)

    sequence = 0
    produced: list[dict[str, Any]] = []

    for capture_file in files:
        if capture_file.validation_errors:
            continue
        if capture_file.kind in (FileKind.VIDEO_360, FileKind.VIDEO_FLAT):
            count, skipped = _extract_video_frames(
                db, storage, session, capture_file, interval, sequence
            )
            produced.append(
                {
                    "file_id": capture_file.id,
                    "kind": "video",
                    "frames": count,
                    # Reported, not silent: a walkthrough that yields far fewer frames than its
                    # duration implies has a reason, and "the operator was standing still" is a
                    # different problem from "extraction is broken".
                    "skipped_stationary": skipped,
                    "interval_seconds": interval,
                }
            )
            sequence += count
        else:
            created = _create_still_frame(db, storage, session, capture_file, sequence)
            if created:
                produced.append({"file_id": capture_file.id, "kind": "image", "frames": 1})
                sequence += 1

    if sequence == 0:
        raise ProcessingError(
            "No frames could be extracted. Check that the uploaded files are valid 360 images "
            "or videos."
        )

    session.frame_count = _count_frames(db, session.id)
    session.file_count = len(
        list(db.scalars(select(CaptureFile.id).where(CaptureFile.session_id == session.id)))
    )
    db.flush()

    log_event(
        "processing.frames_extracted",
        session_id=session.id,
        frame_count=sequence,
        interval_seconds=interval,
    )
    return {"frame_count": sequence, "interval_seconds": interval, "files": produced}


#: Key prefixes that hold *derived* artefacts. Anything outside these is an original upload and
#: must never be deleted by a reprocess.
_DERIVED_KIND_SEGMENTS = ("/frames/", "/thumbs/")


def _is_derived(key: str, source_key: str) -> bool:
    """True only for objects this pipeline generated, never for an original upload.

    This guard exists because a still frame's ``storage_key`` deliberately *is* its source
    object — storing a second full-resolution copy of every uploaded panorama would double
    storage for no benefit. The consequence is that naive cleanup deletes the user's upload:
    reprocessing a capture would destroy the very file it was reprocessing.

    Two independent checks, because losing a customer's only copy of a site capture is not a
    recoverable mistake: the key must differ from the source, *and* it must live under a
    derived-artefact prefix.
    """
    if not key or key == source_key:
        return False
    return any(segment in key for segment in _DERIVED_KIND_SEGMENTS)


def _delete_frames_for_file(db: Session, capture_file: CaptureFile) -> None:
    """Remove this stage's previous output so re-running replaces rather than duplicates."""
    storage = get_storage()
    existing = list(db.scalars(select(CaptureFrame).where(CaptureFrame.file_id == capture_file.id)))
    for frame in existing:
        for key in (
            frame.storage_key,
            frame.thumbnail_key,
            frame.preview_key,
            frame.feature_blob_key,
        ):
            if key and _is_derived(key, capture_file.storage_key):
                with contextlib.suppress(Exception):
                    storage.delete(key)
    if existing:
        db.execute(delete(CaptureFrame).where(CaptureFrame.file_id == capture_file.id))
        db.flush()


def _to_equirectangular(image: np.ndarray, capture_file: CaptureFile) -> np.ndarray:
    """Stitch a dual-fisheye frame; leave anything else exactly as it is.

    Keyed on the *file's* recorded projection rather than re-detecting per frame. Detection is a
    heuristic, and running it on every frame of a video would let it flicker — some frames
    stitched, some not, inside one walkthrough — which is far worse than being consistently wrong
    about the file.
    """
    if capture_file.projection is not ProjectionType.DUAL_FISHEYE:
        return image
    return stitch_to_equirectangular(image)


def _frame_projection(capture_file: CaptureFile) -> ProjectionType:
    """What the *frame* is, which is not always what the file is.

    A dual-fisheye file yields equirectangular frames, because stitching is what the extraction
    stage does to it. Recording the file's own projection here would label the output as raw
    camera data and send every later stage back to reading it as two circles.
    """
    if capture_file.projection is ProjectionType.DUAL_FISHEYE:
        return ProjectionType.EQUIRECTANGULAR
    return capture_file.projection


def _create_still_frame(
    db: Session,
    storage: StorageProvider,
    session: CaptureSession,
    capture_file: CaptureFile,
    sequence: int,
) -> bool:
    data = storage.read_bytes(capture_file.storage_key)
    image = _to_equirectangular(media.decode_image(data), capture_file)
    frame_id = new_id()

    thumb, preview = media.make_derivatives(image)
    ext = media.WEBP_EXTENSION
    thumb_key = session_key(
        session.project_id, session.floor_id, session.id, "thumbs", f"{frame_id}{ext}"
    )
    preview_key = session_key(
        session.project_id, session.floor_id, session.id, "frames", f"{frame_id}_preview{ext}"
    )
    storage.put_bytes(thumb_key, thumb, media.WEBP_CONTENT_TYPE)
    storage.put_bytes(preview_key, preview, media.WEBP_CONTENT_TYPE)

    height, width = image.shape[:2]
    db.add(
        CaptureFrame(
            id=frame_id,
            session_id=session.id,
            file_id=capture_file.id,
            sequence_index=sequence,
            source_time_seconds=0.0,
            # A still's full-resolution frame *is* the uploaded file; storing a second copy would
            # double storage for no benefit.
            storage_key=capture_file.storage_key,
            thumbnail_key=thumb_key,
            preview_key=preview_key,
            width_px=width,
            height_px=height,
            projection=_frame_projection(capture_file),
            captured_at=capture_file.captured_at or session.started_at,
        )
    )
    db.flush()
    return True


def _extract_video_frames(
    db: Session,
    storage: StorageProvider,
    session: CaptureSession,
    capture_file: CaptureFile,
    interval_seconds: float,
    sequence_start: int,
) -> tuple[int, int]:
    """Extract frames from one video, skipping the stretches where the operator stood still.

    Returns ``(kept, skipped)``.

    An operator pauses constantly — to open a door, to look at something, to talk to somebody —
    and at a one-second interval a thirty-second pause is thirty near-identical panoramas. Each
    would cost three stored objects, a quality assessment and a filmstrip slot, and contribute
    nothing: the mapper already discards them as near-duplicates, so they are dead weight by the
    time anything looks at them.

    Dropping them here rather than flagging them later is safe *for video specifically*, because
    the source file is kept: re-processing at a different interval reconstructs whatever was
    skipped. An uploaded still is the opposite — each one is a picture somebody deliberately took
    — so nothing like this applies to :func:`_create_still_frame`.

    The comparison is against the last frame *kept*, not the last one seen. Comparing to the last
    seen would let a slow drift accumulate frame by frame without any single step crossing the
    threshold, and the operator would walk out of shot without a single frame being recorded.
    """
    suffix = Path(capture_file.original_filename).suffix or ".mp4"
    base_time = capture_file.captured_at or session.started_at
    count = 0
    skipped = 0
    kept_hash: str | None = None
    engine = get_vision_engine()

    with _staged_file(storage, capture_file.storage_key, suffix) as path:
        for offset_seconds, image in media.iter_video_frames(
            path, interval_seconds=interval_seconds
        ):
            image = _to_equirectangular(image, capture_file)
            phash = engine.perceptual_hash(image)
            if kept_hash is not None and hamming_distance(phash, kept_hash) <= DUPLICATE_HAMMING:
                skipped += 1
                continue
            kept_hash = phash

            frame_id = new_id()
            ext = media.WEBP_EXTENSION
            full_key = session_key(
                session.project_id, session.floor_id, session.id, "frames", f"{frame_id}{ext}"
            )
            thumb_key = session_key(
                session.project_id, session.floor_id, session.id, "thumbs", f"{frame_id}{ext}"
            )
            preview_key = session_key(
                session.project_id,
                session.floor_id,
                session.id,
                "frames",
                f"{frame_id}_preview{ext}",
            )

            thumb, preview = media.make_derivatives(image)
            # Unlike a still, a video frame is an image *we* produced, so encoding it as WebP
            # re-encodes nothing the operator gave us -- it is the first and only encode.
            storage.put_bytes(
                full_key,
                media.encode_webp(image, media.WEBP_QUALITY_FULL),
                media.WEBP_CONTENT_TYPE,
            )
            storage.put_bytes(thumb_key, thumb, media.WEBP_CONTENT_TYPE)
            storage.put_bytes(preview_key, preview, media.WEBP_CONTENT_TYPE)

            height, width = image.shape[:2]
            db.add(
                CaptureFrame(
                    id=frame_id,
                    session_id=session.id,
                    file_id=capture_file.id,
                    sequence_index=sequence_start + count,
                    source_time_seconds=round(offset_seconds, 3),
                    storage_key=full_key,
                    thumbnail_key=thumb_key,
                    preview_key=preview_key,
                    width_px=width,
                    height_px=height,
                    projection=_frame_projection(capture_file),
                    captured_at=base_time + timedelta(seconds=offset_seconds),
                )
            )
            count += 1
            if count % 25 == 0:
                db.flush()

    db.flush()
    return count, skipped


def _count_frames(db: Session, session_id: str) -> int:
    from sqlalchemy import func

    return int(
        db.scalar(select(func.count(CaptureFrame.id)).where(CaptureFrame.session_id == session_id))
        or 0
    )


# ---- 4. QUALITY --------------------------------------------------------------
def stage_quality(db: Session, session: CaptureSession, spec: JobSpec) -> dict[str, Any]:
    """Score every frame and flag near-duplicates (§10)."""
    storage = get_storage()
    engine = get_vision_engine()
    frames = _session_frames(db, session)
    if not frames:
        raise ProcessingError("No frames to assess — run frame extraction first")

    verdict_counts: dict[str, int] = dict.fromkeys((str(x) for x in QualityVerdict), 0)
    duplicates = 0
    previous_hash: str | None = None
    previous_frame_id: str | None = None

    for frame in frames:
        image = media.decode_image(storage.read_bytes(frame.storage_key))
        metrics = engine.assess_quality(image, projection=frame.projection)
        phash = engine.perceptual_hash(image)
        frame.perceptual_hash = phash

        is_duplicate = False
        duplicate_of = None
        if previous_hash and hamming_distance(phash, previous_hash) <= DUPLICATE_HAMMING:
            is_duplicate = True
            duplicate_of = previous_frame_id
            duplicates += 1
            metrics.warnings.append(
                "Near-duplicate of the previous frame — the camera barely moved"
            )
        else:
            previous_hash = phash
            previous_frame_id = frame.id

        # A duplicate or unusable frame is kept and viewable, but excluded from mapping.
        frame.is_keyframe = metrics.is_usable and not is_duplicate

        existing = db.scalar(select(QualityResult).where(QualityResult.frame_id == frame.id))
        if existing is not None:
            db.delete(existing)
            db.flush()

        db.add(
            QualityResult(
                frame_id=frame.id,
                quality_score=metrics.quality_score,
                blur_score=metrics.blur_score,
                brightness_score=metrics.brightness_score,
                contrast_score=metrics.contrast_score,
                resolution_score=metrics.resolution_score,
                completeness_score=metrics.completeness_score,
                verdict=metrics.verdict,
                is_usable=metrics.is_usable,
                is_duplicate=is_duplicate,
                duplicate_of_frame_id=duplicate_of,
                warnings=metrics.warnings,
                raw_metrics=metrics.raw,
            )
        )
        verdict_counts[str(metrics.verdict)] += 1

    db.flush()
    usable = sum(1 for frame in frames if frame.is_keyframe)
    return {
        "assessed": len(frames),
        "verdicts": verdict_counts,
        "duplicates": duplicates,
        "keyframes": usable,
    }


# ---- 5. FEATURES -------------------------------------------------------------
def stage_features(db: Session, session: CaptureSession, spec: JobSpec) -> dict[str, Any]:
    """Detect and persist ORB features for every keyframe (§15)."""
    storage = get_storage()
    engine = get_vision_engine()
    frames = [frame for frame in _session_frames(db, session) if frame.is_keyframe]

    if not frames:
        return {
            "skipped": True,
            "reason": "No usable keyframes — every frame was unusable or a duplicate",
        }

    total = 0
    for frame in frames:
        image = media.decode_image(storage.read_bytes(frame.storage_key))
        analysis_image = media.resize_to_width(image, MAPPING_ANALYSIS_WIDTH)
        features = engine.detect_features(analysis_image, projection=frame.projection)

        key = session_key(
            session.project_id, session.floor_id, session.id, "frames", f"{frame.id}_orb.npz"
        )
        storage.put_bytes(key, serialize_features(features), "application/octet-stream")
        frame.feature_blob_key = key
        frame.feature_count = features.count
        total += features.count

    db.flush()
    return {
        "frames": len(frames),
        "total_features": total,
        "mean_features": round(total / len(frames), 1),
        "analysis_width": MAPPING_ANALYSIS_WIDTH,
    }


# ---- 6. MAPPING --------------------------------------------------------------
def stage_mapping(db: Session, session: CaptureSession, spec: JobSpec) -> dict[str, Any]:
    """Estimate a relative track, honestly (§14, §46).

    Refuses to publish a path it does not believe. When confidence is below the configured
    threshold the session is marked ``requires_manual_placement`` and the reason is stored — the
    UI then shows "Automatic mapping confidence: Low — manual placement recommended" instead of
    a plausible-looking line through the building.
    """
    storage = get_storage()
    engine = get_vision_engine()
    frames = [
        frame
        for frame in _session_frames(db, session)
        if frame.is_keyframe and frame.feature_blob_key
    ]

    if len(frames) < 2:
        session.mapping_confidence = MappingConfidence.NONE
        session.mapping_confidence_score = None
        session.mapping_note = (
            "Automatic mapping needs at least two usable frames from one walkthrough. "
            "Place this capture on the floor plan manually."
        )
        db.flush()
        return {
            "frames": len(frames),
            "confidence": "none",
            "requires_manual_placement": True,
            "note": session.mapping_note,
        }

    features: list[FeatureSet] = []
    intervals: list[float] = []
    hashes: list[str] = []
    for index, frame in enumerate(frames):
        if not frame.feature_blob_key:
            continue
        features.append(deserialize_features(storage.read_bytes(frame.feature_blob_key)))
        # Already computed by the FEATURES stage for near-duplicate rejection, so loop closure
        # adds no hashing work. An empty string simply never shortlists a candidate.
        hashes.append(frame.perceptual_hash or "")
        if index > 0:
            delta = (frame.captured_at - frames[index - 1].captured_at).total_seconds()
            intervals.append(max(delta, 0.1))

    # Loop closure runs here rather than being an opt-in: a walkthrough that revisits a place has
    # information the odometry chain cannot use, and leaving it on the floor means publishing a
    # track with more drift than the imagery supports. `estimate_track_with_slam` returns the
    # odometry outcome unchanged when there is nothing to close, so the non-looping case is
    # identical to before.
    outcome, slam = engine.estimate_track_with_slam(features, intervals, hashes)

    for pose in outcome.poses:
        if pose.frame_index >= len(frames):
            continue
        frame = frames[pose.frame_index]
        frame.rel_x = pose.x
        frame.rel_y = pose.y
        frame.heading_deg = pose.heading_deg
        frame.position_provenance = DataProvenance.ESTIMATED
        frame.position_confidence = pose.cumulative_confidence

    session.mapping_confidence = outcome.confidence
    session.mapping_confidence_score = outcome.confidence_score
    session.mapping_note = outcome.note
    slam_report = slam.to_dict()
    session.mapping_method = str(slam_report["method"])
    # Stored only when loop closure actually did something, so a null column means "no revisits"
    # rather than "an empty report".
    session.loop_closure_report = None if slam.unchanged else slam_report
    db.flush()

    log_event(
        "mapping.completed",
        session_id=session.id,
        confidence=str(outcome.confidence),
        confidence_score=round(outcome.confidence_score, 4),
        requires_manual_placement=outcome.requires_manual_placement,
        mapping_method=session.mapping_method,
        loop_closures=len(slam.closures),
        loop_closures_rejected=slam.report.loop_closures_rejected,
    )

    return {
        "frames": len(frames),
        "confidence": str(outcome.confidence),
        "confidence_score": round(outcome.confidence_score, 4),
        "requires_manual_placement": outcome.requires_manual_placement,
        "note": outcome.note,
        "per_pair": outcome.per_pair,
        "normalized_track": engine.map_builder.normalize_track(outcome.poses),
        "mapping_method": session.mapping_method,
        "loop_closure": slam_report,
    }


# ---- 7. VIEWER_DATA ----------------------------------------------------------
def stage_viewer_data(db: Session, session: CaptureSession, spec: JobSpec) -> dict[str, Any]:
    """Prepare per-frame viewer data and the capture path (§11, §13)."""
    frames = _session_frames(db, session)
    if not frames:
        raise ProcessingError("No frames available for viewer preparation")

    # Open each frame facing the direction of travel, so clicking "next" feels like walking
    # forwards rather than being spun around.
    for index, frame in enumerate(frames):
        if frame.rel_x is None or index + 1 >= len(frames):
            frame.initial_yaw_deg = 0.0
            continue
        nxt = frames[index + 1]
        if nxt.rel_x is None or nxt.rel_y is None:
            frame.initial_yaw_deg = 0.0
            continue
        dx = nxt.rel_x - frame.rel_x
        dy = (nxt.rel_y or 0.0) - (frame.rel_y or 0.0)
        if abs(dx) < 1e-9 and abs(dy) < 1e-9:
            frame.initial_yaw_deg = 0.0
        else:
            frame.initial_yaw_deg = float(np.degrees(np.arctan2(dx, dy)))

    steps = build_capture_path(db, session)
    db.flush()

    return {
        "frames": len(frames),
        "path_steps": len(steps),
        "located_frames": sum(1 for frame in frames if frame.location_id),
    }


def build_capture_path(db: Session, session: CaptureSession) -> list[dict[str, Any]]:
    """Rebuild the session's capture path from whatever placements currently exist (§13).

    Ordered by timestamp, as the brief specifies. Called by the pipeline and again whenever a
    frame is assigned to a location, so the drawn path always matches the data.
    """
    frames = _session_frames(db, session)
    steps: list[dict[str, Any]] = []
    for frame in frames:
        if not frame.location_id or not frame.location:
            continue
        location = frame.location
        steps.append(
            {
                "location_id": location.id,
                "frame_id": frame.id,
                "code": location.code,
                "x": location.x,
                "y": location.y,
                "timestamp": frame.captured_at.isoformat(),
            }
        )
    steps.sort(key=lambda step: step["timestamp"])

    existing = db.scalar(select(CapturePath).where(CapturePath.session_id == session.id))
    provenance = (
        DataProvenance.MANUAL
        if all(
            frame.location and frame.location.provenance == DataProvenance.MANUAL
            for frame in frames
            if frame.location_id
        )
        else DataProvenance.ESTIMATED
    )

    if existing is None:
        db.add(
            CapturePath(
                session_id=session.id,
                floor_id=session.floor_id,
                steps=steps,
                provenance=provenance,
                confidence=session.mapping_confidence_score,
            )
        )
    else:
        existing.steps = steps
        existing.provenance = provenance
        existing.confidence = session.mapping_confidence_score
    db.flush()
    return steps


def _session_frames(db: Session, session: CaptureSession) -> list[CaptureFrame]:
    return list(
        db.scalars(
            select(CaptureFrame)
            .where(CaptureFrame.session_id == session.id)
            .order_by(CaptureFrame.sequence_index)
        )
    )


# ---- registry ----------------------------------------------------------------
STAGE_HANDLERS = {
    JobType.VALIDATE: stage_validate,
    JobType.METADATA: stage_metadata,
    JobType.FRAME_EXTRACTION: stage_frame_extraction,
    JobType.QUALITY: stage_quality,
    JobType.FEATURES: stage_features,
    JobType.MAPPING: stage_mapping,
    JobType.VIEWER_DATA: stage_viewer_data,
}

#: Session status shown while each stage runs (§34).
STAGE_SESSION_STATUS = {
    JobType.VALIDATE: "processing",
    JobType.METADATA: "processing",
    JobType.FRAME_EXTRACTION: "extracting_frames",
    JobType.QUALITY: "analyzing",
    JobType.FEATURES: "analyzing",
    JobType.MAPPING: "mapping",
    JobType.VIEWER_DATA: "mapping",
}
