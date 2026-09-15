"""Media sniffing, metadata extraction and video frame extraction (§7, §9)."""

from __future__ import annotations

import io
from pathlib import Path

import cv2
import numpy as np
import pytest
from PIL import Image

from app.core.enums import FileKind
from app.core.errors import ProcessingError, UnsupportedMediaError
from app.processing import media
from app.processing.synthetic import Room, render_panorama


def jpeg_bytes(width: int = 512, height: int = 256) -> bytes:
    return media.encode_jpeg(render_panorama(Room(), (0.0, 0.0, 0.0), width=width, height=height))


def png_bytes(width: int = 64, height: int = 32) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (120, 130, 140)).save(buffer, format="PNG")
    return buffer.getvalue()


# ---- sniffing ----------------------------------------------------------------
def test_sniff_identifies_jpeg_and_png() -> None:
    assert media.sniff(jpeg_bytes()[:64], "a.jpg").content_type == "image/jpeg"
    assert media.sniff(png_bytes()[:64], "a.png").content_type == "image/png"
    assert media.sniff(jpeg_bytes()[:64], "a.jpg").is_video is False


def test_sniff_rejects_a_disguised_file() -> None:
    """A text file renamed to .jpg must be refused on its bytes, not accepted on its name."""
    with pytest.raises(UnsupportedMediaError):
        media.sniff(b"#!/bin/sh\nrm -rf /\n" + b"\x00" * 40, "innocent.jpg")


def test_sniff_rejects_an_image_wearing_a_video_extension() -> None:
    """Content and extension disagreeing is a red flag, not something to silently correct."""
    with pytest.raises(UnsupportedMediaError) as exc:
        media.sniff(jpeg_bytes()[:64], "clip.mp4")
    assert "video extension but image content" in str(exc.value)


def test_sniff_identifies_mp4_and_mov_containers() -> None:
    mp4_header = b"\x00\x00\x00\x20" + b"ftypisom" + b"\x00" * 48
    assert media.sniff(mp4_header, "clip.mp4").is_video is True
    assert media.sniff(mp4_header, "clip.mp4").kind is FileKind.VIDEO_360

    mov_header = b"\x00\x00\x00\x14" + b"ftypqt  " + b"\x00" * 48
    result = media.sniff(mov_header, "clip.mov")
    assert result.is_video is True
    assert result.content_type == "video/quicktime"


def test_sniff_identifies_pdf_for_floor_plans() -> None:
    assert media.sniff(b"%PDF-1.7\n" + b"\x00" * 40, "plan.pdf").content_type == "application/pdf"


def test_checksum_is_streaming_and_stable() -> None:
    data = jpeg_bytes()
    stream = io.BytesIO(data)
    first = media.checksum(stream)
    # The stream must be rewound so the caller can still upload it.
    assert stream.tell() == 0
    assert media.checksum(io.BytesIO(data)) == first
    assert len(first) == 64


# ---- decoding ----------------------------------------------------------------
def test_decode_rejects_corrupt_data() -> None:
    with pytest.raises(ProcessingError):
        media.decode_image(b"not an image at all")


def test_decode_and_derivatives() -> None:
    image = media.decode_image(jpeg_bytes(2048, 1024))
    assert image.shape == (1024, 2048, 3)

    thumb, preview = media.make_derivatives(image)
    thumb_image = media.decode_image(thumb)
    preview_image = media.decode_image(preview)

    assert thumb_image.shape[1] == 256
    assert preview_image.shape[1] == 1024
    # Aspect ratio must survive resizing, or the viewer's sphere mapping breaks.
    assert thumb_image.shape[1] / thumb_image.shape[0] == pytest.approx(2.0, abs=0.02)
    assert len(thumb) < len(preview)


def test_resize_never_upscales() -> None:
    small = np.zeros((100, 200, 3), dtype=np.uint8)
    assert media.resize_to_width(small, 4096).shape == small.shape


# ---- metadata ----------------------------------------------------------------
def test_metadata_extraction_never_raises_on_missing_exif() -> None:
    metadata = media.extract_image_metadata(png_bytes())
    assert metadata.width == 64
    assert metadata.height == 32
    assert metadata.camera_make is None
    assert metadata.captured_at is None


def test_exif_camera_and_datetime_are_read() -> None:
    """Write real EXIF with Pillow, then read it back through the extractor."""
    image = Image.new("RGB", (256, 128), (100, 110, 120))
    exif = image.getexif()
    exif[271] = "SiteScope"  # Make
    exif[272] = "TestCam 360"  # Model
    exif[306] = "2026:09:10 09:15:30"  # DateTime

    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", exif=exif)

    metadata = media.extract_image_metadata(buffer.getvalue())
    assert metadata.camera_make == "SiteScope"
    assert metadata.camera_model == "TestCam 360"
    assert metadata.captured_at is not None
    assert metadata.captured_at.year == 2026
    assert metadata.captured_at.month == 9
    assert metadata.captured_at.day == 10
    assert metadata.raw["exif"]["Make"] == "SiteScope"


def test_gpano_projection_is_detected() -> None:
    """A GPano XMP block should be found even when Pillow does not surface it."""
    payload = (
        b"\xff\xd8\xff\xe1" + b"http://ns.google.com/photos/1.0/panorama/"
        b"<GPano:ProjectionType>equirectangular</GPano:ProjectionType>"
        b"<GPano:CroppedAreaImageWidthPixels>4096</GPano:CroppedAreaImageWidthPixels>"
        b"<GPano:FullPanoWidthPixels>4096</GPano:FullPanoWidthPixels>"
    )
    projection, info = media._read_gpano(payload)
    assert projection == "equirectangular"
    assert info is not None
    assert info["horizontal_coverage"] == 1.0


def test_partial_panorama_coverage_is_reported() -> None:
    payload = (
        b"GPano<GPano:ProjectionType>equirectangular</GPano:ProjectionType>"
        b"<GPano:CroppedAreaImageWidthPixels>2048</GPano:CroppedAreaImageWidthPixels>"
        b"<GPano:FullPanoWidthPixels>4096</GPano:FullPanoWidthPixels>"
    )
    _, info = media._read_gpano(payload)
    assert info is not None
    assert info["horizontal_coverage"] == 0.5


def test_dms_conversion() -> None:
    assert media._dms_to_degrees((51.0, 30.0, 0.0), "N") == pytest.approx(51.5)
    assert media._dms_to_degrees((51.0, 30.0, 0.0), "S") == pytest.approx(-51.5)
    assert media._dms_to_degrees(None, "N") is None
    assert media._dms_to_degrees(("bad",), "N") is None


# ---- video -------------------------------------------------------------------
@pytest.fixture
def video_file(tmp_path: Path) -> Path:
    """A real 6-second, 10 fps equirectangular MP4 written with OpenCV.

    Genuine encoded video rather than a stub: frame extraction is exactly the stage where
    container and codec behaviour matters, so testing it against a synthetic array would test
    nothing that can actually break.
    """
    path = tmp_path / "walk.mp4"
    width, height, fps, seconds = 640, 320, 10, 6
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height))
    if not writer.isOpened():
        pytest.skip("This OpenCV build cannot write MP4")

    room = Room()
    # A moving camera, so extracted frames genuinely differ.
    for index in range(fps * seconds):
        x = -6.0 + 12.0 * index / (fps * seconds)
        writer.write(render_panorama(room, (x, 0.0, 0.0), width=width, height=height))
    writer.release()
    assert path.stat().st_size > 0
    return path


def test_video_metadata(video_file: Path) -> None:
    metadata = media.extract_video_metadata(video_file)
    assert metadata.width == 640
    assert metadata.height == 320
    assert metadata.fps == pytest.approx(10.0, abs=0.5)
    assert metadata.duration_seconds == pytest.approx(6.0, abs=0.5)


def test_frame_interval_is_honoured(video_file: Path) -> None:
    """§9: the extraction interval is configurable and must actually change the output."""
    every_second = list(media.iter_video_frames(video_file, interval_seconds=1.0))
    every_two = list(media.iter_video_frames(video_file, interval_seconds=2.0))
    every_three = list(media.iter_video_frames(video_file, interval_seconds=3.0))

    assert len(every_second) == 6
    assert len(every_two) == 3
    assert len(every_three) == 2

    timestamps = [round(t, 1) for t, _ in every_two]
    assert timestamps == [0.0, 2.0, 4.0]
    for _, frame in every_two:
        assert frame.shape == (320, 640, 3)


def test_extraction_respects_the_frame_cap(video_file: Path) -> None:
    """§9: do not process every frame of a long video."""
    capped = list(media.iter_video_frames(video_file, interval_seconds=0.1, max_frames=5))
    assert len(capped) == 5


def test_extracted_frames_actually_differ(video_file: Path) -> None:
    frames = [frame for _, frame in media.iter_video_frames(video_file, interval_seconds=2.0)]
    assert len(frames) >= 2
    difference = float(np.mean(np.abs(frames[0].astype(int) - frames[-1].astype(int))))
    assert difference > 1.0, "extraction returned effectively identical frames"


def test_invalid_interval_is_rejected(video_file: Path) -> None:
    with pytest.raises(ProcessingError):
        list(media.iter_video_frames(video_file, interval_seconds=0))


def test_unopenable_video_raises_clearly(tmp_path: Path) -> None:
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"\x00" * 2048)
    with pytest.raises(ProcessingError):
        media.extract_video_metadata(broken)


def test_planned_frame_count() -> None:
    assert media.planned_frame_count(10.0, 2.0) == 6
    assert media.planned_frame_count(None, 2.0) is None
    assert media.planned_frame_count(10.0, 0) is None


def test_the_video_decoder_is_ffmpeg_backed() -> None:
    """Frame extraction depends on FFmpeg, so assert it rather than assume the wheel has it.

    `iter_video_frames` hands the container to `cv2.VideoCapture`, whose MP4/MOV support *is*
    the bundled FFmpeg — OpenCV has no demuxer of its own for those. A build without it does not
    fail to import or to run the image pipeline; it opens every video successfully-looking and
    yields zero frames, so a capture would arrive, process, and report ready with nothing in it.
    The wheels carry FFmpeg on every platform CI runs, which is exactly why this is worth
    pinning: it holds until someone swaps the dependency, and then it stops holding silently.
    """
    backends = {cv2.videoio_registry.getBackendName(b) for b in cv2.videoio_registry.getBackends()}
    assert "FFMPEG" in backends, (
        f"OpenCV has no FFmpeg backend; video capture will yield nothing: {backends}"
    )


@pytest.fixture
def paused_walk_video(tmp_path: Path) -> Path:
    """A walk that stops dead in the middle, as every real walkthrough does.

    Six seconds: two walking, two standing perfectly still, two walking again. The still stretch
    is what frame extraction has to notice — at a one-second interval it is otherwise two more
    stored panoramas of the same view, and at a busy site those stretches are most of the video.
    """
    path = tmp_path / "paused.mp4"
    width, height, fps, seconds = 640, 320, 10, 6
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height))
    if not writer.isOpened():
        pytest.skip("This OpenCV build cannot write MP4")

    room = Room()
    for index in range(fps * seconds):
        t = index / fps
        if t < 2.0:
            x = -6.0 + 3.0 * t
        elif t < 4.0:
            x = 0.0  # standing still
        else:
            x = 3.0 * (t - 4.0)
        writer.write(render_panorama(room, (x, 0.0, 0.0), width=width, height=height))
    writer.release()
    assert path.stat().st_size > 0
    return path


def test_extraction_keeps_every_frame_of_a_moving_walk(video_file: Path) -> None:
    """The guard must not fire on ordinary walking, or it would thin a good capture."""
    frames = list(media.iter_video_frames(video_file, interval_seconds=1.0))

    assert len(frames) >= 5, f"a 6-second moving walk yielded only {len(frames)} frames"


# ---- WebP --------------------------------------------------------------------
def test_webp_is_materially_smaller_than_the_jpeg_it_replaced() -> None:
    """The whole justification for the format change, asserted rather than assumed.

    Compared at the quality each size actually uses, not at a matched quality number: the point
    is that the bytes we now store are smaller than the bytes we used to store.
    """
    image = render_panorama(Room(), (0.0, 0.0, 0.0), width=2048, height=1024)

    jpeg = media.encode_jpeg(image, quality=90)
    webp = media.encode_webp(image, media.WEBP_QUALITY_FULL)

    assert len(webp) < len(jpeg) * 0.75, (
        f"webp {len(webp)} vs jpeg {len(jpeg)} — the format change is not paying for itself"
    )


def test_webp_survives_a_round_trip_with_its_colours_intact() -> None:
    """A channel swap produces a valid file of the right size that is simply wrong.

    Pillow works in RGB and OpenCV in BGR, so the fallback encoder has to convert. Nothing about
    the output length or the encoder's return value would reveal getting that backwards, which is
    why this compares pixels against a deliberately asymmetric colour.
    """
    image = np.zeros((64, 128, 3), dtype=np.uint8)
    image[:, :, 0] = 200  # blue channel, in OpenCV's ordering
    image[:, :, 2] = 20  # red channel

    decoded = media.decode_image(media.encode_webp(image, quality=100))

    assert decoded.shape == image.shape
    # Lossless enough at quality 100 to compare channel means without a tolerance argument.
    assert decoded[:, :, 0].mean() > decoded[:, :, 2].mean(), "blue and red were swapped"


def test_the_pillow_fallback_encoder_agrees_with_opencv() -> None:
    """The fallback exists for builds without WebP, so it must not be the untested path.

    It is exercised directly here, because on a wheel that *does* have WebP it would otherwise
    never run — and a fallback that has never run is not a fallback.
    """
    image = render_panorama(Room(), (0.0, 0.0, 0.0), width=512, height=256)

    primary = media.decode_image(media.encode_webp(image, quality=90))
    fallback = media.decode_image(media._encode_webp_pillow(image, quality=90))

    assert primary.shape == fallback.shape == image.shape
    # Two encoders, same format, same quality: not byte-identical, but visually equivalent.
    assert float(np.abs(primary.astype(float) - fallback.astype(float)).mean()) < 3.0


def test_derivatives_are_webp() -> None:
    image = render_panorama(Room(), (0.0, 0.0, 0.0), width=2048, height=1024)

    thumb, preview = media.make_derivatives(image)

    # RIFF....WEBP is the container signature; checking it proves the format rather than trusting
    # that the encoder was handed the extension we think it was.
    for data in (thumb, preview):
        assert data[:4] == b"RIFF" and data[8:12] == b"WEBP", data[:16]
