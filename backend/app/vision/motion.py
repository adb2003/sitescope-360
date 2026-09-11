"""Relative motion estimation between two equirectangular frames (§15).

What this does: recovers the **rotation** and the **direction of translation** between two
panoramas, by decomposing the spherical essential matrix.

What it cannot do: recover *how far* the camera moved. A single moving camera gives no metric
scale — that is a property of monocular geometry, not a limitation of this implementation. The
platform therefore treats step length as an assumption (constant pace × elapsed time) and labels
every derived position ``estimated`` with a confidence, never ``measured``.
"""

from __future__ import annotations

from typing import Protocol

import numpy as np

from app.vision.panorama import pixels_to_bearings
from app.vision.types import MatchResult, RelativeMotion

#: Below this median parallax the two views are effectively a pure rotation, and the direction of
#: translation is mathematically unrecoverable. Detecting it is what stops the mapper from
#: inventing a walk when the operator only turned around.
MIN_PARALLAX_DEG = 0.35
#: Inliers needed before a pose is worth reporting at all.
MIN_INLIERS = 20
#: Inliers at which the count stops adding confidence.
SATURATION_INLIERS = 80
#: Cheirality margin over the runner-up candidate at which the decomposition is unambiguous.
#: Below it the winner is decided by numerical noise rather than by the data, so the pair is
#: refused outright rather than published with reduced confidence - see :func:`_decompose`.
MIN_AMBIGUITY_MARGIN = 0.25

_W = np.array([[0.0, -1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]])


class MotionEstimator(Protocol):
    def estimate(self, matches: MatchResult, width: int, height: int) -> RelativeMotion: ...


class EssentialMotionEstimator:
    """Essential-matrix decomposition with cheirality disambiguation."""

    def estimate(self, matches: MatchResult, width: int, height: int) -> RelativeMotion:
        reasons: list[str] = []
        inliers = matches.inlier_count

        if inliers < 8:
            return _no_motion(
                matches, f"only {inliers} verified matches (8 required for two-view geometry)"
            )

        b1 = pixels_to_bearings(matches.points_a, width, height)
        b2 = pixels_to_bearings(matches.points_b, width, height)

        # Degeneracy check FIRST. Under pure rotation the essential matrix is identically zero,
        # so its SVD returns numerical noise — a rotation-only fit is the only trustworthy way
        # to recognise that case. Decomposing first and inspecting the result afterwards would
        # mean judging degeneracy with the very numbers the degeneracy corrupted.
        rotation_only, parallax_deg = _rotation_only_fit(b1, b2)
        if rotation_only is None:
            return _no_motion(matches, "rotation could not be fitted to the correspondences")

        if parallax_deg < MIN_PARALLAX_DEG:
            return RelativeMotion(
                translation=np.zeros(3),
                rotation=rotation_only,
                yaw_deg=_yaw_from_rotation(rotation_only),
                confidence=0.0,
                inlier_count=inliers,
                match_count=matches.raw_match_count,
                reasons=[
                    f"median parallax {parallax_deg:.2f}° is below {MIN_PARALLAX_DEG}° — "
                    "this looks like rotation in place, so translation is unrecoverable"
                ],
            )

        essential = _essential_from_bearings(b1, b2)
        if essential is None:
            return _no_motion(matches, "essential matrix could not be estimated")

        rotation, translation, positive_depth_ratio, ambiguity_margin = _decompose(
            essential, b1, b2
        )
        if rotation is None or translation is None:
            return _no_motion(matches, "essential matrix decomposition was degenerate")

        if ambiguity_margin < MIN_AMBIGUITY_MARGIN:
            # Refused rather than merely penalised. The four candidates differ by a sign on the
            # translation, so a near-tie does not mean "slightly less accurate" - it means the
            # direction of travel is undetermined, and the wrong pick is roughly 180 degrees out.
            # Dead reckoning would then carry that error through every later frame. The rotation
            # from the Procrustes fit is still the well-determined part, so it is kept.
            return RelativeMotion(
                translation=np.zeros(3),
                rotation=rotation_only,
                yaw_deg=_yaw_from_rotation(rotation_only),
                confidence=0.0,
                inlier_count=inliers,
                match_count=matches.raw_match_count,
                reasons=[
                    f"the four-fold essential-matrix decomposition is near-tied (margin "
                    f"{ambiguity_margin:.1%}, needs {MIN_AMBIGUITY_MARGIN:.0%}) - the direction "
                    f"of travel is not determined by this frame pair"
                ],
            )

        confidence = self._confidence(
            inlier_ratio=matches.inlier_ratio,
            inliers=inliers,
            parallax_deg=parallax_deg,
            positive_depth_ratio=positive_depth_ratio,
            reasons=reasons,
        )

        return RelativeMotion(
            translation=translation,
            rotation=rotation,
            yaw_deg=_yaw_from_rotation(rotation),
            confidence=confidence,
            inlier_count=inliers,
            match_count=matches.raw_match_count,
            reasons=reasons,
        )

    def _confidence(
        self,
        *,
        inlier_ratio: float,
        inliers: int,
        parallax_deg: float,
        positive_depth_ratio: float,
        reasons: list[str],
    ) -> float:
        ratio_term = float(np.clip((inlier_ratio - 0.15) / 0.55, 0.0, 1.0))
        count_term = float(np.clip(inliers / SATURATION_INLIERS, 0.0, 1.0))
        parallax_term = float(np.clip((parallax_deg - MIN_PARALLAX_DEG) / 3.0, 0.0, 1.0))
        cheirality_term = float(np.clip((positive_depth_ratio - 0.6) / 0.35, 0.0, 1.0))

        if inliers < MIN_INLIERS:
            reasons.append(f"low inlier count ({inliers})")
        if inlier_ratio < 0.25:
            reasons.append(f"low inlier ratio ({inlier_ratio:.0%}) — repetitive scene texture")
        if parallax_deg < 1.0:
            reasons.append(f"small parallax ({parallax_deg:.2f}°) — frames are close together")
        if positive_depth_ratio < 0.75:
            reasons.append(
                f"only {positive_depth_ratio:.0%} of points triangulate in front of both views"
            )

        # Geometric mean: every term must hold up. One weak axis should not be averaged away.
        terms = np.array([ratio_term, count_term, parallax_term, cheirality_term])
        if float(terms.min()) <= 0.0:
            return 0.0
        return float(np.exp(np.mean(np.log(terms))))


# ---- geometry ----------------------------------------------------------------
def _essential_from_bearings(b1: np.ndarray, b2: np.ndarray) -> np.ndarray | None:
    a = np.einsum("ni,nj->nij", b2, b1).reshape(b1.shape[0], 9)
    try:
        _, _, vt = np.linalg.svd(a)
        essential = vt[-1].reshape(3, 3)
        u, _, vt2 = np.linalg.svd(essential)
    except np.linalg.LinAlgError:
        return None
    return u @ np.diag([1.0, 1.0, 0.0]) @ vt2


def _decompose(
    essential: np.ndarray, b1: np.ndarray, b2: np.ndarray
) -> tuple[np.ndarray | None, np.ndarray | None, float, float]:
    """Pick the one of four (R, t) candidates that puts most points in front of both cameras.

    Convention: camera 1 at the origin with identity rotation; a world point ``X`` projects into
    camera 2 as ``R X + t``, so ``E = [t]_× R``. The returned ``translation`` is the unit
    direction of camera 2's centre expressed in camera 1's frame, which is what the position
    estimator integrates.

    Returns ``(rotation, translation, cheirality_ratio, ambiguity_margin)``.

    The **margin** — the gap between the winning candidate's cheirality ratio and the runner-up's
    — matters as much as the winner, and :meth:`EssentialMotionEstimator.estimate` refuses the
    pair outright when it falls below :data:`MIN_AMBIGUITY_MARGIN`. When two candidates score
    almost equally the choice is genuinely undetermined by the data and floating-point noise
    decides it. A flipped ``t`` is not slightly wrong, it is roughly 180° wrong, and dead
    reckoning then carries that error through every later frame — so this is a disqualifier, not
    a deduction from a score.

    On the demo walkthrough's real matches every pair scores 0.86–1.00, so the gate does not fire
    in practice. It was written while investigating a platform-dependent accuracy difference on
    the hypothesis that a near-tie explained it; measurement showed it does not. The gate is kept
    because the failure mode it prevents is real and silent, not because it explained that bug.
    """
    try:
        u, _, vt = np.linalg.svd(essential)
    except np.linalg.LinAlgError:
        return None, None, 0.0, 0.0

    if np.linalg.det(u) < 0:
        u = -u
    if np.linalg.det(vt) < 0:
        vt = -vt

    r_a = u @ _W @ vt
    r_b = u @ _W.T @ vt
    t_unit = u[:, 2]

    best: tuple[np.ndarray, np.ndarray] | None = None
    best_ratio = -1.0
    runner_up_ratio = -1.0
    for rotation in (r_a, r_b):
        if abs(np.linalg.det(rotation) - 1.0) > 1e-6:
            continue
        for sign in (1.0, -1.0):
            t = sign * t_unit
            ratio = _cheirality_ratio(rotation, t, b1, b2)
            if ratio > best_ratio:
                runner_up_ratio = best_ratio
                best_ratio, best = ratio, (rotation, t)
            elif ratio > runner_up_ratio:
                runner_up_ratio = ratio

    if best is None:
        return None, None, 0.0, 0.0

    margin = float(max(best_ratio - max(runner_up_ratio, 0.0), 0.0))

    rotation, t = best
    # Direction of camera 2's centre, in camera 1's coordinates.
    centre_direction = -rotation.T @ t
    norm = float(np.linalg.norm(centre_direction))
    if norm < 1e-9:
        return rotation, None, best_ratio, margin
    return rotation, centre_direction / norm, float(max(best_ratio, 0.0)), margin


def _cheirality_ratio(rotation: np.ndarray, t: np.ndarray, b1: np.ndarray, b2: np.ndarray) -> float:
    """Fraction of correspondences triangulating to positive depth in both views."""
    depths1, depths2 = _triangulate_depths(rotation, t, b1, b2)
    if depths1.size == 0:
        return 0.0
    return float(np.mean((depths1 > 0) & (depths2 > 0)))


def _triangulate_depths(
    rotation: np.ndarray, t: np.ndarray, b1: np.ndarray, b2: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Least-squares depths along each bearing pair.

    Solves ``λ1 (R b1) − λ2 b2 = −t`` per correspondence — three equations, two unknowns.
    Vectorised via the 2×2 normal equations so a 1000-match pair costs microseconds.
    """
    rb1 = b1 @ rotation.T  # (n, 3)
    a11 = np.einsum("ni,ni->n", rb1, rb1)
    a12 = -np.einsum("ni,ni->n", rb1, b2)
    a22 = np.einsum("ni,ni->n", b2, b2)
    rhs1 = -np.einsum("ni,i->n", rb1, t)
    rhs2 = np.einsum("ni,i->n", b2, t)

    det = a11 * a22 - a12 * a12
    safe = np.abs(det) > 1e-12
    lambda1 = np.zeros_like(det)
    lambda2 = np.zeros_like(det)
    lambda1[safe] = (a22[safe] * rhs1[safe] - a12[safe] * rhs2[safe]) / det[safe]
    lambda2[safe] = (a11[safe] * rhs2[safe] - a12[safe] * rhs1[safe]) / det[safe]
    return lambda1, lambda2


def _rotation_only_fit(b1: np.ndarray, b2: np.ndarray) -> tuple[np.ndarray | None, float]:
    """Best rotation explaining the correspondences, plus the parallax it cannot explain.

    Orthogonal Procrustes: minimise ``Σ‖R·b1ᵢ − b2ᵢ‖²`` over rotations. Closed form via SVD of
    the cross-covariance, with reflection suppressed.

    The returned angle is the median residual after the best rotation is removed. If the camera
    only turned, that residual is ~0 and the rotation is exact. If the camera also moved, the
    residual is the parallax — which is precisely the signal that makes translation recoverable.
    """
    if b1.shape[0] < 3:
        return None, 0.0
    try:
        u, _, vt = np.linalg.svd(b2.T @ b1)
    except np.linalg.LinAlgError:
        return None, 0.0
    correction = np.diag([1.0, 1.0, float(np.sign(np.linalg.det(u @ vt)) or 1.0)])
    rotation = u @ correction @ vt

    residual_deg = _median_parallax_deg(rotation, b1, b2)
    return rotation, residual_deg


def _median_parallax_deg(rotation: np.ndarray, b1: np.ndarray, b2: np.ndarray) -> float:
    """Angle between each bearing after removing rotation, and its match.

    With rotation compensated, any remaining angular disparity is caused by translation. Its
    median is a direct, interpretable measure of "did the camera actually move".
    """
    rotated = b1 @ rotation.T
    dots = np.clip(np.einsum("ni,ni->n", rotated, b2), -1.0, 1.0)
    return float(np.degrees(np.median(np.arccos(dots))))


def _yaw_from_rotation(rotation: np.ndarray) -> float:
    """Heading change about the vertical (+Y) axis, in degrees."""
    return float(np.degrees(np.arctan2(rotation[0, 2], rotation[2, 2])))


def _no_motion(matches: MatchResult, reason: str) -> RelativeMotion:
    return RelativeMotion(
        translation=np.zeros(3),
        rotation=np.eye(3),
        yaw_deg=0.0,
        confidence=0.0,
        inlier_count=matches.inlier_count,
        match_count=matches.raw_match_count,
        reasons=[reason],
    )
