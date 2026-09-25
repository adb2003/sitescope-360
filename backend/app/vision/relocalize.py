"""Recognising where a new walk was taken, against walks already placed (§15, §46).

Anchoring (`app.services.alignment`) asks an operator to name two frames and say where on the
floor plan each was taken. It works, and it has to be done again for every capture of the same
floor — the fiftieth walk down the same corridor is placed by hand exactly like the first.

This module removes that repetition for any floor that has been captured before. The earlier
capture's frames are already sitting on plan coordinates; if a frame of the new walk was taken
*in the same place* as one of them, that old frame's plan position is an anchor for the new one.
Enough of those and the similarity transform that anchoring needed can be fitted without a human
supplying anything.

The whole feature therefore rests on one question — **was this photograph taken from the same
spot as that one?** — and not on the weaker question the rest of the vision code asks, which is
whether two frames *overlap*. Two frames of the same corridor twenty metres apart overlap
enormously and share hundreds of features. Treating that as a position would place the new walk
twenty metres from where it happened, and nothing downstream could tell.

What separates the two is **parallax**. Photographs taken from one spot differ only by rotation:
every correspondence is explained by a single rotation matrix, and the residual parallax is
essentially zero. Move the camera and that stops being true immediately. So a match is accepted
as a position only when:

* enough features match and survive spherical epipolar verification (appearance alone is worth
  nothing on a construction site — identical bays, repeated formwork, parallel corridors);
* the correspondences are spread across the frame, not clustered on one blank wall, because
  low parallax measured over a tight cluster is arithmetic on nothing;
* a rotation-only fit explains them with little parallax left over.

`app.vision.slam` asks the same question to recognise a revisit inside one walkthrough, but it
asks it far more strictly, and the difference is the point. A walk that loops back stands on the
point it stood on before, so slam can demand 0.3° — a centimetre or two. An operator walking the
floor a week later does not: they walk roughly the same route and stand roughly where they stood,
and a threshold that admits nothing but a centimetre would recognise nothing at all. So
`MAX_PARALLAX_DEG` here is a viewpoint tolerance rather than an identity test, and what it buys
in reach it gives up in precision — see the constant for what the number means and does not mean.

**Two anchors are not enough to check.** A similarity transform has four degrees of freedom and
two correspondences supply exactly four, so the fit passes through both points exactly and the
residual is zero *whatever the anchors are* — including when both are wrong. With three the
residual finally measures something. Manual anchoring can accept two because a person looked at
the plan; nothing looked at these, so `MIN_TRANSFORM_MATCHES` is three.

Nothing here touches the database or the ORM: it takes feature sets and plan coordinates and
returns matches, so it can be tested against known geometry without a capture pipeline.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import numpy as np

from app.vision.motion import _rotation_only_fit, _yaw_from_rotation
from app.vision.panorama import pixels_to_bearings
from app.vision.slam import (
    COLOCATED_INLIER_MULTIPLIER,
    MAX_HASH_DISTANCE,
    MIN_LOOP_INLIERS,
    _is_well_spread,
    hamming_distance,
)
from app.vision.types import FeatureSet, MatchResult

#: Verified inliers before a pair may be read as "the same place".
#:
#: The co-located bar from loop closure, and for the same reason: asserting a position outright
#: is a stronger claim than asserting a direction, so it needs more evidence. A wrong closure
#: distorts one track; a wrong relocalisation places an entire capture in the wrong part of the
#: building and labels it estimated rather than guessed.
MIN_MATCH_INLIERS = int(MIN_LOOP_INLIERS * COLOCATED_INLIER_MULTIPLIER)

#: Residual parallax below which two frames are treated as the same point, in degrees.
#:
#: **Deliberately not** `slam.MAX_COLOCATED_PARALLAX_DEG`, which is 0.3°. That constant asks
#: whether a walk returned to a point it had already stood on, where the honest answer is yes or
#: no and the camera really was in the same place. This asks a different question: whether an
#: operator walking the floor a week later stood close enough to last week's spot to be placed
#: there. They did not stand in the same place, and holding them to 0.3° would mean recognising
#: nothing — parallax is an angle, so at an indoor working depth of around 3 m, 0.3° is 1.6 cm of
#: movement.
#:
#: 8° is about half a metre at that depth. The conversion is the honest part of this constant and
#: also its weakness: parallax measures an angle, monocular vision has no scale, and the same 8°
#: is 0.5 m against a wall 3 m away and 1.7 m across a 12 m atrium. So this is a bound on
#: *similarity of viewpoint*, and the metres are an illustration, not a measurement.
#:
#: What makes that acceptable is the company it keeps: three such anchors are required, their
#: residual is checked against a similarity fit, and the resulting positions are labelled
#: `estimated`. What it is protecting against is the failure that matters — a frame twenty metres
#: down the same corridor, which shares hundreds of features and reads as tens of degrees.
MAX_PARALLAX_DEG = 8.0

#: Hamming distance between 64-bit dHashes below which a reference is worth verifying at all.
#: Loose on purpose — this only shortlists, geometry decides — but it is what keeps the cost
#: linear in a floor's history rather than quadratic.
MAX_SHORTLIST_DISTANCE = MAX_HASH_DISTANCE

#: References verified per query frame, most similar-looking first.
#:
#: All of them are verified, not just until one passes: the accepted match is the one with the
#: least parallax, and that cannot be known without measuring the alternatives. The cap is what
#: keeps that from growing with a floor's history.
MAX_CANDIDATES_PER_FRAME = 6

#: Matches needed before the whole track may be placed by a fitted transform. Three, not two:
#: see the module docstring — a two-point similarity fit has a zero residual by construction and
#: so carries no evidence that it is right.
MIN_TRANSFORM_MATCHES = 3

#: Floor-plan pixels two anchors must be apart before the pair constrains rotation and scale.
#: Anchors on top of each other leave the fit free to rotate the building about them.
MIN_ANCHOR_SEPARATION_PX = 40.0


class Matcher(Protocol):
    def match(self, a: FeatureSet, b: FeatureSet) -> MatchResult: ...


@dataclass(frozen=True, slots=True)
class ReferenceFrame:
    """A frame from an earlier capture whose position on the floor plan is already known."""

    frame_id: str
    session_id: str
    #: Floor-plan pixels.
    plan_x: float
    plan_y: float
    perceptual_hash: str
    #: The location row the frame is bound to, so a recognised frame can join it rather than
    #: creating a second point a few pixels away for the same physical spot.
    location_id: str
    #: Fetches the descriptors, which live in object storage rather than the database.
    #:
    #: A callable and not a `FeatureSet` because the hash shortlist runs first and discards most
    #: of a floor's history: a busy floor accumulates thousands of placed frames, and reading
    #: every blob to compare against six of them would make the cost of recognition grow with
    #: everything that ever happened on that floor.
    load_features: Callable[[], FeatureSet]


@dataclass(frozen=True, slots=True)
class PlaceMatch:
    """One query frame recognised as standing where a reference frame stood."""

    query_index: int
    reference_frame_id: str
    reference_session_id: str
    location_id: str
    plan_x: float
    plan_y: float
    inlier_count: int
    parallax_deg: float
    hash_distance: int
    #: Yaw of the new frame relative to the reference, degrees. Not used for placement — kept
    #: because it is the only orientation evidence that survives between sessions.
    yaw_offset_deg: float
    confidence: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "query_index": self.query_index,
            "reference_frame_id": self.reference_frame_id,
            "reference_session_id": self.reference_session_id,
            "location_id": self.location_id,
            "plan_x": round(self.plan_x, 2),
            "plan_y": round(self.plan_y, 2),
            "inlier_count": self.inlier_count,
            "parallax_deg": round(self.parallax_deg, 4),
            "hash_distance": self.hash_distance,
            "yaw_offset_deg": round(self.yaw_offset_deg, 2),
            "confidence": round(self.confidence, 4),
        }


@dataclass(frozen=True, slots=True)
class RecognitionReport:
    """What was compared, what was believed, and what was thrown away."""

    matches: list[PlaceMatch] = field(default_factory=list)
    references_considered: int = 0
    pairs_verified: int = 0
    rejected_few_inliers: int = 0
    rejected_clustered: int = 0
    rejected_parallax: int = 0
    rejected_duplicate_reference: int = 0

    @property
    def anchor_span_px(self) -> float:
        """Largest distance between any two accepted matches, in plan pixels."""
        if len(self.matches) < 2:
            return 0.0
        points = np.array([[match.plan_x, match.plan_y] for match in self.matches])
        diffs = points[:, None, :] - points[None, :, :]
        return float(np.sqrt((diffs**2).sum(axis=-1)).max())

    def to_dict(self) -> dict[str, Any]:
        return {
            "matches": [match.to_dict() for match in self.matches],
            "matched_frames": len(self.matches),
            "references_considered": self.references_considered,
            "pairs_verified": self.pairs_verified,
            "anchor_span_px": round(self.anchor_span_px, 1),
            "rejected": {
                "few_inliers": self.rejected_few_inliers,
                "clustered_correspondences": self.rejected_clustered,
                "parallax": self.rejected_parallax,
                "duplicate_reference": self.rejected_duplicate_reference,
            },
        }


class Relocalizer:
    """Matches a new capture's frames against frames already placed on the same floor."""

    def __init__(
        self,
        matcher: Matcher,
        *,
        min_inliers: int = MIN_MATCH_INLIERS,
        max_parallax_deg: float = MAX_PARALLAX_DEG,
        max_shortlist_distance: int = MAX_SHORTLIST_DISTANCE,
        max_candidates_per_frame: int = MAX_CANDIDATES_PER_FRAME,
    ) -> None:
        self.matcher = matcher
        self.min_inliers = min_inliers
        self.max_parallax_deg = max_parallax_deg
        self.max_shortlist_distance = max_shortlist_distance
        self.max_candidates_per_frame = max_candidates_per_frame

    def recognise(
        self,
        queries: list[FeatureSet],
        hashes: list[str],
        references: list[ReferenceFrame],
    ) -> RecognitionReport:
        """Find which query frames stand where a reference frame stood.

        ``hashes`` are the perceptual hashes the FEATURES stage already computed, positionally
        aligned with ``queries``; an empty string simply shortlists nothing for that frame.
        """
        if not queries or not references or len(queries) != len(hashes):
            return RecognitionReport(references_considered=len(references))

        report_counts = {
            "verified": 0,
            "few_inliers": 0,
            "clustered": 0,
            "parallax": 0,
        }
        best_per_query: dict[int, PlaceMatch] = {}

        for index, (features, digest) in enumerate(zip(queries, hashes, strict=True)):
            if features.count < 2:
                continue
            for reference, distance in self._shortlist(digest, references):
                report_counts["verified"] += 1
                match = self._verify(index, features, reference, distance, report_counts)
                if match is None:
                    continue
                # Every surviving candidate is verified, and the *least* parallax wins — not the
                # first to pass, and not the closest hash.
                #
                # The tolerance is around a metre, and capture points on a walkthrough are often
                # spaced about that far apart, so two adjacent reference points can both clear
                # the bar for one query frame. Taking whichever happened to hash closest would
                # then place the frame at its neighbour, and an off-by-one shared by every frame
                # of the walk is a pure translation — which a similarity transform reproduces
                # exactly, residual and all, leaving nothing downstream to notice it. Parallax is
                # the only signal here that orders candidates by *distance*, so it decides.
                held = best_per_query.get(index)
                if held is None or match.parallax_deg < held.parallax_deg:
                    best_per_query[index] = match

        accepted, duplicates = _resolve_duplicate_references(best_per_query)
        return RecognitionReport(
            matches=accepted,
            references_considered=len(references),
            pairs_verified=report_counts["verified"],
            rejected_few_inliers=report_counts["few_inliers"],
            rejected_clustered=report_counts["clustered"],
            rejected_parallax=report_counts["parallax"],
            rejected_duplicate_reference=duplicates,
        )

    def _shortlist(
        self, digest: str, references: list[ReferenceFrame]
    ) -> list[tuple[ReferenceFrame, int]]:
        """References worth verifying for one query frame, closest-looking first."""
        if not digest:
            return []
        scored = [
            (reference, hamming_distance(digest, reference.perceptual_hash))
            for reference in references
        ]
        near = [pair for pair in scored if pair[1] <= self.max_shortlist_distance]
        near.sort(key=lambda pair: pair[1])
        return near[: self.max_candidates_per_frame]

    def _verify(
        self,
        index: int,
        features: FeatureSet,
        reference: ReferenceFrame,
        hash_distance: int,
        counts: dict[str, int],
    ) -> PlaceMatch | None:
        """Decide whether one (query, reference) pair is a position or merely a resemblance."""
        matches = self.matcher.match(features, reference.load_features())
        if matches.inlier_count < self.min_inliers:
            counts["few_inliers"] += 1
            return None

        width, height = features.image_width, features.image_height
        if not _is_well_spread(matches.points_a, width, height):
            counts["clustered"] += 1
            return None

        bearings_a = pixels_to_bearings(matches.points_a, width, height)
        bearings_b = pixels_to_bearings(matches.points_b, width, height)
        rotation, parallax_deg = _rotation_only_fit(bearings_a, bearings_b)
        if rotation is None or parallax_deg > self.max_parallax_deg:
            counts["parallax"] += 1
            return None

        return PlaceMatch(
            query_index=index,
            reference_frame_id=reference.frame_id,
            reference_session_id=reference.session_id,
            location_id=reference.location_id,
            plan_x=reference.plan_x,
            plan_y=reference.plan_y,
            inlier_count=matches.inlier_count,
            parallax_deg=parallax_deg,
            hash_distance=hash_distance,
            yaw_offset_deg=_yaw_from_rotation(rotation),
            confidence=_confidence(matches.inlier_count, parallax_deg, self.max_parallax_deg),
        )


def _confidence(inlier_count: int, parallax_deg: float, max_parallax_deg: float) -> float:
    """How much agreed, and how still it was. Neither alone is evidence of a position."""
    support = float(np.clip(inlier_count / (MIN_MATCH_INLIERS * 2.0), 0.0, 1.0))
    stillness = float(np.clip(1.0 - parallax_deg / max(max_parallax_deg, 1e-9), 0.0, 1.0))
    return float(np.sqrt(support * stillness))


def _resolve_duplicate_references(
    best_per_query: dict[int, PlaceMatch],
) -> tuple[list[PlaceMatch], int]:
    """Let one reference anchor one query frame only, keeping the better-evidenced claim.

    Two frames of the same walk cannot both have been taken at one point, and allowing it would
    hand the transform two correspondences with identical targets — which is not an anchor pair
    at all: it pins a translation and leaves rotation and scale free to be whatever fits the
    remaining noise.
    """
    by_reference: dict[str, PlaceMatch] = {}
    dropped = 0
    for match in sorted(best_per_query.values(), key=lambda item: item.query_index):
        held = by_reference.get(match.reference_frame_id)
        if held is None:
            by_reference[match.reference_frame_id] = match
        elif match.confidence > held.confidence:
            by_reference[match.reference_frame_id] = match
            dropped += 1
        else:
            dropped += 1
    accepted = sorted(by_reference.values(), key=lambda item: item.query_index)
    return accepted, dropped


def anchors_are_usable(matches: list[PlaceMatch]) -> tuple[bool, str]:
    """Can these matches support a transform over the whole track, and if not, why not.

    Separate from the fit itself because the reasons are worth stating precisely: an operator
    told "automatic placement was not possible" learns nothing, and the difference between *not
    recognised* and *recognised in one spot only* decides what they should do next.
    """
    if len(matches) < MIN_TRANSFORM_MATCHES:
        return False, (
            f"Recognised {len(matches)} frame(s) from earlier captures of this floor; "
            f"{MIN_TRANSFORM_MATCHES} are needed to place the whole walk. A two-point fit "
            "matches its own anchors exactly, so it cannot be checked."
        )

    points = np.array([[match.plan_x, match.plan_y] for match in matches])
    diffs = points[:, None, :] - points[None, :, :]
    span = float(np.sqrt((diffs**2).sum(axis=-1)).max())
    if span < MIN_ANCHOR_SEPARATION_PX:
        return False, (
            f"The {len(matches)} recognised frames are all within {span:.0f}px of each other on "
            "the plan, which fixes a position but leaves rotation and scale undetermined."
        )
    return True, f"{len(matches)} frames recognised, spanning {span:.0f}px of the plan."
