"""Feature detection and matching (§15, §31).

ORB is the default because it is free of patent restrictions, ships in the base OpenCV wheel, and
is fast enough to run on every frame of a long walkthrough. ``FeatureDetector`` is an interface,
so SIFT (where licensing permits), AKAZE or a learned detector can be substituted without
touching the pipeline.
"""

from __future__ import annotations

import io
from typing import Protocol

import cv2
import numpy as np

from app.core.config import settings
from app.core.enums import ProjectionType
from app.vision.panorama import equator_band_mask, pixels_to_bearings
from app.vision.types import FeatureSet, MatchResult

#: Lowe's ratio. 0.75 is the usual compromise; lower is stricter.
LOWE_RATIO = 0.75
#: Below this the geometry solver has nothing to work with.
MIN_MATCHES_FOR_GEOMETRY = 12


class FeatureDetector(Protocol):
    def detect(self, image: np.ndarray, projection: ProjectionType) -> FeatureSet: ...


class OrbFeatureDetector:
    """ORB detector, restricted to the equatorial band for equirectangular input."""

    def __init__(self, n_features: int | None = None, equator_fraction: float = 0.55) -> None:
        self.n_features = int(n_features or settings.orb_features)
        self.equator_fraction = equator_fraction
        self._orb = cv2.ORB_create(  # type: ignore[attr-defined]  # missing from cv2 stubs
            nfeatures=self.n_features,
            scaleFactor=1.2,
            nlevels=8,
            edgeThreshold=31,
            fastThreshold=12,
        )

    def detect(
        self,
        image: np.ndarray,
        projection: ProjectionType = ProjectionType.EQUIRECTANGULAR,
    ) -> FeatureSet:
        if image is None or image.size == 0:
            return _empty_featureset(0, 0, projection)

        height, width = image.shape[:2]
        grey = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image

        mask = None
        if projection is ProjectionType.EQUIRECTANGULAR:
            mask = equator_band_mask(width, height, self.equator_fraction)

        keypoints, descriptors = self._orb.detectAndCompute(grey, mask)
        if not keypoints or descriptors is None:
            return _empty_featureset(width, height, projection)

        points = np.array([kp.pt for kp in keypoints], dtype=np.float32)
        return FeatureSet(
            keypoints=points,
            descriptors=np.asarray(descriptors, dtype=np.uint8),
            image_width=width,
            image_height=height,
            projection=projection,
        )


class FeatureMatcher(Protocol):
    def match(self, a: FeatureSet, b: FeatureSet) -> MatchResult: ...


class BruteForceMatcher:
    """Hamming brute force + Lowe ratio test + spherical geometric verification.

    A plain ratio test on a construction site produces many wrong matches, because scaffolding,
    formwork and ceiling tiles are visually repetitive. Verification against the two-view
    epipolar geometry on the unit sphere removes them, and the surviving inlier ratio is the
    single most informative input to the mapping confidence.
    """

    def __init__(self, ratio: float = LOWE_RATIO) -> None:
        self.ratio = ratio
        self._matcher = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=False)

    def match(self, a: FeatureSet, b: FeatureSet) -> MatchResult:
        if a.count < 2 or b.count < 2:
            return MatchResult(_empty2(), _empty2(), 0, 0)

        knn = self._matcher.knnMatch(a.descriptors, b.descriptors, k=2)
        points_a: list[np.ndarray] = []
        points_b: list[np.ndarray] = []
        for pair in knn:
            if len(pair) < 2:
                continue
            best, second = pair
            if best.distance < self.ratio * second.distance:
                points_a.append(a.keypoints[best.queryIdx])
                points_b.append(b.keypoints[best.trainIdx])

        if not points_a:
            return MatchResult(_empty2(), _empty2(), 0, 0)

        pa = np.asarray(points_a, dtype=np.float64)
        pb = np.asarray(points_b, dtype=np.float64)
        raw_count = int(pa.shape[0])

        if raw_count < MIN_MATCHES_FOR_GEOMETRY:
            # Not enough to verify. Return them unverified with inlier_count 0, so the caller's
            # confidence calculation reflects that nothing was confirmed.
            return MatchResult(pa, pb, raw_count, 0)

        inliers = verify_spherical(pa, pb, a.image_width, a.image_height)
        return MatchResult(
            points_a=pa[inliers],
            points_b=pb[inliers],
            raw_match_count=raw_count,
            inlier_count=int(inliers.sum()),
        )


def verify_spherical(
    points_a: np.ndarray,
    points_b: np.ndarray,
    width: int,
    height: int,
    *,
    threshold_deg: float = 1.5,
    iterations: int = 400,
    seed: int = 12345,
) -> np.ndarray:
    """RANSAC over the spherical essential matrix; returns a boolean inlier mask.

    For calibrated bearing vectors the epipolar constraint is ``b2ᵀ E b1 = 0``. That formulation
    needs no pinhole model, which is exactly why it works on a full 360 sphere where a
    perspective homography or ``cv2.findEssentialMat`` would be invalid (points behind the
    camera have no image plane projection).
    """
    b1 = pixels_to_bearings(points_a, width, height)
    b2 = pixels_to_bearings(points_b, width, height)
    n = b1.shape[0]
    if n < 8:
        return np.zeros(n, dtype=bool)

    rng = np.random.default_rng(seed)
    threshold = np.sin(np.deg2rad(threshold_deg))
    best_mask = np.zeros(n, dtype=bool)
    best_count = 0

    for _ in range(iterations):
        sample = rng.choice(n, size=8, replace=False)
        essential = _eight_point(b1[sample], b2[sample])
        if essential is None:
            continue
        residual = _sampson_like(essential, b1, b2)
        mask = residual < threshold
        count = int(mask.sum())
        if count > best_count:
            best_count, best_mask = count, mask
            if best_count > 0.9 * n:
                break

    # Refit on the consensus set, then re-classify — a standard RANSAC polish step that
    # typically recovers another 5–15% of true inliers.
    if best_count >= 8:
        refined = _eight_point(b1[best_mask], b2[best_mask])
        if refined is not None:
            residual = _sampson_like(refined, b1, b2)
            polished = residual < threshold
            if int(polished.sum()) >= best_count:
                best_mask = polished
    return best_mask


def _eight_point(b1: np.ndarray, b2: np.ndarray) -> np.ndarray | None:
    """Linear least-squares essential matrix from >= 8 bearing correspondences."""
    if b1.shape[0] < 8:
        return None
    # Each row encodes b2ᵀ E b1 = 0 with E flattened row-major.
    a = np.einsum("ni,nj->nij", b2, b1).reshape(b1.shape[0], 9)
    try:
        _, _, vt = np.linalg.svd(a)
    except np.linalg.LinAlgError:
        return None
    essential = vt[-1].reshape(3, 3)

    # Project onto the essential manifold: two equal singular values, one zero.
    try:
        u, _, vt2 = np.linalg.svd(essential)
    except np.linalg.LinAlgError:
        return None
    return u @ np.diag([1.0, 1.0, 0.0]) @ vt2


def _sampson_like(essential: np.ndarray, b1: np.ndarray, b2: np.ndarray) -> np.ndarray:
    """Normalised algebraic residual, comparable to an angular error in radians (small angles)."""
    eb1 = b1 @ essential.T  # (n, 3)
    etb2 = b2 @ essential  # (n, 3)
    numerator = np.abs(np.einsum("ni,ni->n", b2, eb1))
    denominator = np.sqrt(eb1[:, 0] ** 2 + eb1[:, 1] ** 2 + etb2[:, 0] ** 2 + etb2[:, 1] ** 2)
    return numerator / np.maximum(denominator, 1e-12)


# ---- serialisation -----------------------------------------------------------
def serialize_features(features: FeatureSet) -> bytes:
    """Compact ``.npz`` blob written to object storage by the FEATURES stage."""
    buffer = io.BytesIO()
    np.savez_compressed(
        buffer,
        keypoints=features.keypoints.astype(np.float32),
        descriptors=features.descriptors.astype(np.uint8),
        meta=np.array([features.image_width, features.image_height], dtype=np.int32),
        projection=np.array([str(features.projection)]),
    )
    return buffer.getvalue()


def deserialize_features(blob: bytes) -> FeatureSet:
    with np.load(io.BytesIO(blob), allow_pickle=False) as data:
        meta = data["meta"]
        projection_raw = str(data["projection"][0])
        try:
            projection = ProjectionType(projection_raw)
        except ValueError:
            projection = ProjectionType.UNKNOWN
        return FeatureSet(
            keypoints=np.asarray(data["keypoints"], dtype=np.float32),
            descriptors=np.asarray(data["descriptors"], dtype=np.uint8),
            image_width=int(meta[0]),
            image_height=int(meta[1]),
            projection=projection,
        )


def _empty_featureset(width: int, height: int, projection: ProjectionType) -> FeatureSet:
    return FeatureSet(
        keypoints=np.zeros((0, 2), dtype=np.float32),
        descriptors=np.zeros((0, 32), dtype=np.uint8),
        image_width=width,
        image_height=height,
        projection=projection,
    )


def _empty2() -> np.ndarray:
    return np.zeros((0, 2), dtype=np.float64)
