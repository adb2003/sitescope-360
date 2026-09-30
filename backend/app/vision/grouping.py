"""Finding where one upload stops being one walkthrough (§9, §46).

An operator who captures a whole building in a morning does not stop and file paperwork between
floors. They walk level 0, take the lift, walk level 1, take the lift, walk level 2, and upload
the lot. The platform then has one ``CaptureSession``, which has exactly one ``floor_id``, and
mapping estimates a single continuous track through three walks that share no space at all. The
positions either side of each join are not merely imprecise; they are answers to a question that
was never asked.

This module finds the joins. It answers one question per consecutive pair of frames — *did the
walk continue here?* — from two signals, and it needs both because each alone is wrong in a way
the other corrects.

**The clock.** A gap between frames means the camera stopped. But an absolute threshold cannot
work for both inputs the platform accepts: video sampled every two seconds has no gaps at all by
construction, while hand-taken stills are irregular by nature and a twenty-second pause between
two of them is unremarkable. So a gap is measured against the session's *own* cadence — the
median interval it has been keeping — and only counts when it is also long in absolute terms.

**The imagery.** Consecutive frames of a continuing walk overlap enormously; they are a step
apart. Frames either side of a lift ride share nothing. That is a far stronger signal than the
clock, and it is already computed: the MAPPING stage matches every consecutive pair to estimate
the track, so the inlier counts exist before this module runs and it does no vision work of its
own. It reads the arithmetic and decides.

**Why neither alone.** An operator who stops for five minutes to talk to a foreman, then carries
on down the same corridor, produces a large gap and no discontinuity — the imagery refutes the
clock, and it should, because the walk did continue. An operator who steps through a doorway from
a bare concrete shell into a fitted-out office produces a discontinuity with no gap; that is a
real break in the imagery and worth proposing, but on its own it is also what a blank white wall
produces, so it needs one more check.

**What cannot be told apart from what is not there.** Two frames with nothing in common are
evidence of a break only if there was something to have in common. A dark stairwell or a plain
plastered wall yields almost no ORB features, and matching nothing against nothing also produces
no inliers. Reporting that as a break would split hardest exactly where the imagery is weakest.
So a pair whose frames are too featureless to judge is recorded as **unknown**, not as a break,
and unknown is only ever proposed as a boundary when the clock independently agrees.

**What this module will not do, ever.** It does not know that the second segment is level 1.
Nothing in the pixels or the timestamps says so, and no amount of algorithm will make it say so:
the building's floors are a fact about the world, not about the imagery. So this proposes
boundaries with the evidence behind each one and stops there. A person assigns the floors, and
`app.services.grouping` applies what they confirm. A feature that split and labelled
automatically would be inventing the one part it cannot know — which is precisely what §46
forbids.

Nothing here touches the database or the ORM.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any

#: Shortest gap that may ever be read as the camera stopping, in seconds.
#:
#: The cadence test alone is too eager at the fast end: video sampled every 0.5 s has a median
#: interval of 0.5 s, so a three-second pause is six times cadence — and three seconds is a
#: person turning round, not a person leaving the floor. This floor is what keeps the ratio from
#: being applied to intervals too short for the multiple to mean anything.
MIN_GAP_SECONDS = 25.0

#: How many times the session's own median interval a gap must be before the clock calls it a
#: stop. Six is deliberately generous: the clock is the weaker of the two signals here and is
#: only ever decisive when the imagery cannot answer, so its job is to miss quietly rather than
#: to split a walk that continued.
GAP_CADENCE_MULTIPLE = 6.0

#: Verified inliers below which a pair carries no geometric relationship in absolute terms.
#:
#: Twelve, matching `features.MIN_MATCHES_FOR_GEOMETRY` — the point at which the solver has
#: nothing to work with. Note how far this sits below `RelativeMotion.is_reliable`'s twenty: a
#: dim stairwell pair at fifteen inliers is a *poor* link, and mapping already says so in its
#: confidence. It is not a break, and splitting a walkthrough there would punish the operator
#: for the lighting.
#:
#: An absolute floor alone is not enough, though, and real imagery is what showed it. Two
#: panoramas of structurally identical spaces with different surfaces — which is what one floor
#: of a building looks like from another — still produce single-figure inlier counts by
#: coincidence, and RANSAC will happily find a consensus among them. Nine matches is not a
#: relationship, but it clears any floor low enough to be defensible on its own.
SCENE_BREAK_INLIERS = 12

#: …so the bar is also a fraction of what *this* walk's own links carry.
#:
#: The same reasoning as the cadence, and for the same reason it uses a median: the question is
#: not whether two frames are related in absolute terms but whether the walk continued at the
#: rate it had been going. Consecutive frames a step apart in a corridor share hundreds of
#: features; the pair either side of a lift ride shares a handful. That is two orders of
#: magnitude, so a tenth of the typical link separates them with room to spare while still
#: adapting to a capture whose imagery is poor throughout.
SCENE_BREAK_FRACTION = 0.10

#: ORB features a frame must yield before "nothing matched" is allowed to mean anything.
#:
#: The detector is asked for `settings.orb_features` (1200 by default) and a frame of an ordinary
#: site returns most of them. A tenth of that is the point below which the frame is carrying
#: almost no texture — a plastered wall, a dark stairwell, a lens against a hoarding — and a
#: failure to match it says more about the frame than about the walk.
MIN_FEATURES_TO_JUDGE = 120

#: Frames a proposed segment must contain to be offered as a walkthrough of its own.
#:
#: Below this a "segment" is more plausibly a hiccup — a lens cap, a few frames of the inside of
#: a pocket — than a walk, and carving it into a separate capture would leave the operator
#: tidying up after the algorithm. Such boundaries are still reported, with this as the reason,
#: because the operator may know better.
MIN_SEGMENT_FRAMES = 4


@dataclass(frozen=True, slots=True)
class Link:
    """What is already known about one consecutive pair of frames.

    Everything here is a by-product of stages that have already run: the interval comes from
    EXIF/extraction timestamps, the counts from the pairwise matching MAPPING does to estimate
    the track, and the feature counts from FEATURES.
    """

    #: Seconds between the two frames' capture times. Never negative.
    gap_seconds: float
    #: Correspondences surviving spherical epipolar verification.
    inlier_count: int
    #: Correspondences surviving the Lowe ratio test, before geometry.
    match_count: int
    #: ORB features detected in each frame, which is what decides whether a failure to match
    #: can be read at all.
    features_before: int
    features_after: int


@dataclass(frozen=True, slots=True)
class Boundary:
    """A place where the walk may have stopped, and everything behind that judgement."""

    #: The break sits between frame ``link_index`` and ``link_index + 1``.
    link_index: int
    gap_seconds: float
    #: The session's median interval, for context — the ratio is only meaningful against it.
    cadence_seconds: float
    inlier_count: int
    match_count: int
    #: The clock's verdict.
    time_break: bool
    #: The imagery's verdict. ``None`` means the frames were too featureless to judge.
    scene_break: bool | None
    #: Whether this is offered to the operator as a split, as opposed to merely recorded.
    proposed: bool
    confidence: float
    reason: str

    @property
    def gap_ratio(self) -> float:
        """How many times the session's own cadence this gap ran to."""
        if self.cadence_seconds <= 0.0:
            return 0.0
        return self.gap_seconds / self.cadence_seconds

    def to_dict(self) -> dict[str, Any]:
        return {
            "link_index": self.link_index,
            "gap_seconds": round(self.gap_seconds, 2),
            "cadence_seconds": round(self.cadence_seconds, 2),
            "gap_ratio": round(self.gap_ratio, 1),
            "inliers": self.inlier_count,
            "matches": self.match_count,
            "time_break": self.time_break,
            "scene_break": self.scene_break,
            "proposed": self.proposed,
            "confidence": round(self.confidence, 3),
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class Segment:
    """A run of consecutive frames that the evidence keeps together."""

    #: Inclusive positions in the sequence the detector was given.
    start_index: int
    end_index: int

    @property
    def frame_count(self) -> int:
        return self.end_index - self.start_index + 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "start_index": self.start_index,
            "end_index": self.end_index,
            "frame_count": self.frame_count,
        }


@dataclass(slots=True)
class GroupingReport:
    """What the detector found, including what it considered and declined."""

    cadence_seconds: float = 0.0
    #: Every pair that tripped either signal, proposed or not — the rejections are the part
    #: worth reading when the answer looks wrong.
    boundaries: list[Boundary] = field(default_factory=list)
    segments: list[Segment] = field(default_factory=list)
    note: str = ""

    @property
    def proposed(self) -> list[Boundary]:
        return [boundary for boundary in self.boundaries if boundary.proposed]

    @property
    def looks_like_one_walkthrough(self) -> bool:
        return len(self.segments) <= 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "cadence_seconds": round(self.cadence_seconds, 2),
            "segments": [segment.to_dict() for segment in self.segments],
            "boundaries": [boundary.to_dict() for boundary in self.boundaries],
            "proposed_count": len(self.proposed),
            "note": self.note,
        }


class WalkthroughDetector:
    """Splits a sequence of frames into the walks it is actually made of.

    The thresholds are constructor arguments so they can be tightened for an operator who
    captures differently, and so tests can state the bar they are exercising instead of
    depending on the module's defaults staying put.
    """

    def __init__(
        self,
        *,
        min_gap_seconds: float = MIN_GAP_SECONDS,
        gap_cadence_multiple: float = GAP_CADENCE_MULTIPLE,
        scene_break_inliers: int = SCENE_BREAK_INLIERS,
        scene_break_fraction: float = SCENE_BREAK_FRACTION,
        min_features_to_judge: int = MIN_FEATURES_TO_JUDGE,
        min_segment_frames: int = MIN_SEGMENT_FRAMES,
    ) -> None:
        self.min_gap_seconds = min_gap_seconds
        self.gap_cadence_multiple = gap_cadence_multiple
        self.scene_break_inliers = scene_break_inliers
        self.scene_break_fraction = scene_break_fraction
        self.min_features_to_judge = min_features_to_judge
        self.min_segment_frames = min_segment_frames

    def detect(self, links: list[Link]) -> GroupingReport:
        """``links`` describes frames ``0..len(links)`` — one entry per consecutive pair."""
        if not links:
            return GroupingReport(
                segments=[],
                note="Nothing to group — a walkthrough needs at least two frames.",
            )

        cadence = self._cadence(links)
        gap_threshold = max(self.min_gap_seconds, cadence * self.gap_cadence_multiple)
        inlier_threshold = self._inlier_threshold(links)

        candidates = [
            boundary
            for index, link in enumerate(links)
            if (boundary := self._judge(index, link, cadence, gap_threshold, inlier_threshold))
            is not None
        ]
        # Demotion happens after every candidate is known, because whether a boundary leaves a
        # viable segment depends on where the *neighbouring* boundaries fell.
        boundaries = self._drop_slivers(candidates, frame_count=len(links) + 1)
        segments = _segments_from(boundaries, frame_count=len(links) + 1)
        return GroupingReport(
            cadence_seconds=cadence,
            boundaries=boundaries,
            segments=segments,
            note=_note(segments, boundaries),
        )

    # ---- one pair ------------------------------------------------------------
    def _judge(
        self,
        index: int,
        link: Link,
        cadence: float,
        gap_threshold: float,
        inlier_threshold: float,
    ) -> Boundary | None:
        time_break = link.gap_seconds >= gap_threshold
        scene_break = self._scene_verdict(link, inlier_threshold)
        # An unknown verdict is worth recording only where something actually looked wrong. A
        # featureless pair that still matched well is an ordinary step through a dim corridor;
        # one that matched nothing is a place where the answer is genuinely unavailable, and an
        # operator looking for a break the detector missed should be shown it rather than have
        # it silently counted as "the walk continued".
        unanswerable = scene_break is None and link.inlier_count < inlier_threshold

        if not time_break and scene_break is not True and not unanswerable:
            return None  # The walk continued; nothing to report.

        proposed = scene_break is True or (time_break and scene_break is None)
        return Boundary(
            link_index=index,
            gap_seconds=link.gap_seconds,
            cadence_seconds=cadence,
            inlier_count=link.inlier_count,
            match_count=link.match_count,
            time_break=time_break,
            scene_break=scene_break,
            proposed=proposed,
            confidence=_confidence(time_break, scene_break, link, inlier_threshold),
            reason=_reason(time_break, scene_break, link, cadence),
        )

    def _scene_verdict(self, link: Link, inlier_threshold: float) -> bool | None:
        """Did the imagery continue? ``None`` when the frames cannot answer."""
        if min(link.features_before, link.features_after) < self.min_features_to_judge:
            return None
        return link.inlier_count < inlier_threshold

    def _inlier_threshold(self, links: list[Link]) -> float:
        """What this walk's own links carry, and what a break must fall short of.

        The median for the same reason the cadence uses one: the breaks being looked for are
        themselves in the sample, and a mean would be dragged *down* by them — lowering the very
        bar meant to catch them, and by more the more floors the upload covered.
        """
        typical = statistics.median([float(link.inlier_count) for link in links])
        return max(float(self.scene_break_inliers), typical * self.scene_break_fraction)

    @staticmethod
    def _cadence(links: list[Link]) -> float:
        """The interval this session has been keeping.

        The median rather than the mean, and for the reason that matters here: the gaps this
        module is looking for are themselves in the sample. A mean would be dragged upward by
        the lift rides, raising the very threshold meant to catch them — the more floors an
        upload covers, the harder it would become to notice.
        """
        intervals = [link.gap_seconds for link in links if link.gap_seconds > 0.0]
        if not intervals:
            return 0.0
        return float(statistics.median(intervals))

    # ---- the whole sequence --------------------------------------------------
    def _drop_slivers(self, candidates: list[Boundary], *, frame_count: int) -> list[Boundary]:
        """Demote boundaries that would carve off a run too short to be a walkthrough."""
        kept: list[Boundary] = []
        previous_end = -1  # Last index of the segment that would precede the next boundary.

        for boundary in candidates:
            if not boundary.proposed:
                kept.append(boundary)
                continue
            if boundary.link_index - previous_end < self.min_segment_frames:
                kept.append(_demote(boundary, self.min_segment_frames))
                continue
            kept.append(boundary)
            previous_end = boundary.link_index

        # And the tail: the final segment runs to the last frame, so the last surviving boundary
        # has to leave enough behind it too. Demoting one promotes its predecessor to last, whose
        # tail is now longer — so this walks backwards rather than checking once.
        for position in range(len(kept) - 1, -1, -1):
            boundary = kept[position]
            if not boundary.proposed:
                continue
            if frame_count - 1 - boundary.link_index >= self.min_segment_frames:
                break
            kept[position] = _demote(boundary, self.min_segment_frames)
        return kept


def _demote(boundary: Boundary, min_segment_frames: int) -> Boundary:
    return Boundary(
        link_index=boundary.link_index,
        gap_seconds=boundary.gap_seconds,
        cadence_seconds=boundary.cadence_seconds,
        inlier_count=boundary.inlier_count,
        match_count=boundary.match_count,
        time_break=boundary.time_break,
        scene_break=boundary.scene_break,
        proposed=False,
        confidence=boundary.confidence,
        reason=(
            f"{boundary.reason} Not proposed: splitting here would leave fewer than "
            f"{min_segment_frames} frames on one side, which is a hiccup rather than a walk."
        ),
    )


def _segments_from(boundaries: list[Boundary], *, frame_count: int) -> list[Segment]:
    cuts = [boundary.link_index for boundary in boundaries if boundary.proposed]
    segments: list[Segment] = []
    start = 0
    for cut in cuts:
        segments.append(Segment(start_index=start, end_index=cut))
        start = cut + 1
    segments.append(Segment(start_index=start, end_index=frame_count - 1))
    return segments


def _confidence(
    time_break: bool, scene_break: bool | None, link: Link, inlier_threshold: float
) -> float:
    """How much of the evidence agrees, weighted by which evidence it is.

    The imagery carries most of the weight because it answers the question directly; the clock
    only ever says the camera stopped, which is compatible with the walk continuing.
    """
    if scene_break is True:
        # How emphatically nothing matched. Zero inliers is a harder break than seven.
        emptiness = 1.0 - (link.inlier_count / max(inlier_threshold, 1.0))
        score = 0.65 + 0.2 * max(emptiness, 0.0)
        return min(score + (0.15 if time_break else 0.0), 1.0)
    if scene_break is None:
        return 0.45 if time_break else 0.2
    return 0.1  # The imagery says the walk continued, whatever the clock said.


def _reason(time_break: bool, scene_break: bool | None, link: Link, cadence: float) -> str:
    clock = (
        f"The camera stopped for {link.gap_seconds:.0f}s against a typical {cadence:.1f}s "
        "between frames."
        if time_break
        else f"No unusual pause — {link.gap_seconds:.0f}s between frames."
    )
    if scene_break is True:
        return (
            f"{clock} The two frames share nothing: {link.inlier_count} verified matches from "
            f"{link.match_count} candidates, so the camera was somewhere else."
        )
    if scene_break is None:
        return (
            f"{clock} Whether the view changed cannot be told — one of the frames yielded too "
            f"little texture to match ({min(link.features_before, link.features_after)} "
            "features), so an empty result means nothing either way."
        )
    return (
        f"{clock} The imagery continues across it — {link.inlier_count} verified matches — so "
        "the walk did too."
    )


def _note(segments: list[Segment], boundaries: list[Boundary]) -> str:
    considered = len(boundaries)
    if len(segments) <= 1:
        if considered:
            return (
                f"This looks like one walkthrough. {considered} interruption(s) were examined "
                "and none of them broke the walk."
            )
        return "This looks like one walkthrough — the frames run continuously throughout."
    sizes = ", ".join(str(segment.frame_count) for segment in segments)
    return (
        f"This upload looks like {len(segments)} separate walkthroughs ({sizes} frames). "
        "Each one needs its own floor before it can be mapped — nothing in the imagery says "
        "which floor, so that part is yours."
    )
