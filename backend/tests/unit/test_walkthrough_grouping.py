"""Deciding where one upload stops being one walkthrough (§9, §46).

The detector is arithmetic over evidence the MAPPING stage already produced, so it can be
exercised exactly: every test here states the situation in frames and inlier counts and asserts
what should be concluded from it. What cannot be checked this way is whether ORB on real
panoramas produces those counts at a real lift ride — `tests/integration/test_capture_split.py`
does that end to end.

The tests that matter most are the ones where the two signals disagree. A detector that splits
whenever the clock jumps would cut a walkthrough in half every time an operator stopped to talk
to somebody, and a detector that splits whenever nothing matched would cut it every time the
camera faced a blank wall. Both are here.
"""

from __future__ import annotations

from app.vision.grouping import (
    MIN_FEATURES_TO_JUDGE,
    MIN_SEGMENT_FRAMES,
    Link,
    WalkthroughDetector,
)

#: A frame pair from a walk that is continuing: a step apart, plenty in common.
CONTINUES = Link(
    gap_seconds=2.0, inlier_count=180, match_count=360, features_before=900, features_after=900
)


def link(
    *,
    gap: float = 2.0,
    inliers: int = 180,
    matches: int = 360,
    features: int = 900,
) -> Link:
    return Link(
        gap_seconds=gap,
        inlier_count=inliers,
        match_count=matches,
        features_before=features,
        features_after=features,
    )


def walk(length: int) -> list[Link]:
    """``length`` consecutive pairs of an uninterrupted walk."""
    return [CONTINUES] * length


# ---- the ordinary case -------------------------------------------------------
def test_an_uninterrupted_walk_is_one_walkthrough() -> None:
    report = WalkthroughDetector().detect(walk(12))

    assert report.looks_like_one_walkthrough
    assert len(report.segments) == 1
    assert report.segments[0].frame_count == 13
    assert report.boundaries == []
    assert "one walkthrough" in report.note


def test_nothing_to_group_is_said_rather_than_guessed() -> None:
    report = WalkthroughDetector().detect([])

    assert report.segments == []
    assert "at least two frames" in report.note


# ---- the case the feature exists for -----------------------------------------
def test_a_lift_ride_splits_the_upload() -> None:
    """The operator stopped recording, rode up, and started again somewhere else."""
    links = [*walk(5), link(gap=180.0, inliers=0, matches=4), *walk(5)]

    report = WalkthroughDetector().detect(links)

    assert len(report.segments) == 2
    assert [segment.frame_count for segment in report.segments] == [6, 6]
    (boundary,) = report.proposed
    assert boundary.link_index == 5
    assert boundary.time_break is True
    assert boundary.scene_break is True
    assert boundary.confidence > 0.75, "both signals agreeing is the strongest case there is"
    assert "separate walkthroughs" in report.note


def test_the_break_is_reported_with_the_evidence_behind_it() -> None:
    links = [*walk(5), link(gap=180.0, inliers=2, matches=9), *walk(5)]

    (boundary,) = WalkthroughDetector().detect(links).proposed

    assert "180s" in boundary.reason
    assert "2 verified matches from 9 candidates" in boundary.reason
    assert boundary.gap_ratio == 90.0  # 180s against a 2s cadence.


# ---- where the two signals disagree ------------------------------------------
def test_a_pause_in_the_same_corridor_is_not_a_break() -> None:
    """The operator stopped to talk to a foreman and then carried on walking.

    The clock says the camera stopped for five minutes. The imagery says the next frame is a
    step from the last one. The imagery is answering the question that was asked.
    """
    links = [*walk(5), link(gap=300.0), *walk(5)]

    report = WalkthroughDetector().detect(links)

    assert report.looks_like_one_walkthrough
    (considered,) = report.boundaries
    assert considered.time_break is True
    assert considered.scene_break is False
    assert considered.proposed is False
    assert "the walk did too" in considered.reason


def test_a_doorway_into_a_different_space_is_a_break_without_any_pause() -> None:
    """Nothing about a break requires the camera to have stopped."""
    links = [*walk(5), link(gap=2.0, inliers=1, matches=6), *walk(5)]

    (boundary,) = WalkthroughDetector().detect(links).proposed

    assert boundary.time_break is False
    assert boundary.scene_break is True
    assert "No unusual pause" in boundary.reason


# ---- what cannot be told apart from what is not there ------------------------
def test_two_featureless_frames_cannot_report_a_break_on_their_own() -> None:
    """A plain wall matches nothing. So does a different building. Only one is a break."""
    blank = MIN_FEATURES_TO_JUDGE - 1
    links = [
        *walk(5),
        Link(
            gap_seconds=2.0,
            inlier_count=0,
            match_count=0,
            features_before=blank,
            features_after=blank,
        ),
        *walk(5),
    ]

    report = WalkthroughDetector().detect(links)

    assert report.looks_like_one_walkthrough
    (considered,) = report.boundaries
    assert considered.scene_break is None
    assert considered.proposed is False
    assert "cannot be told" in considered.reason


def test_a_featureless_pair_is_proposed_when_the_clock_agrees() -> None:
    """Unknown plus a stopped camera is worth asking about, and says so at a lower confidence."""
    blank = MIN_FEATURES_TO_JUDGE - 1
    links = [
        *walk(5),
        Link(
            gap_seconds=400.0,
            inlier_count=0,
            match_count=0,
            features_before=blank,
            features_after=900,
        ),
        *walk(5),
    ]

    (boundary,) = WalkthroughDetector().detect(links).proposed

    assert boundary.scene_break is None
    assert boundary.confidence < 0.6, "one signal, and the weaker one, is not a confident answer"
    assert len(WalkthroughDetector().detect(links).segments) == 2


# ---- the cadence -------------------------------------------------------------
def test_the_cadence_is_the_median_so_the_gaps_cannot_hide_themselves() -> None:
    """A mean would be dragged up by the very gaps being looked for.

    Two five-minute lift rides in a twelve-frame upload lift the mean interval to about a
    minute, and six times that is longer than the rides themselves — so the more floors an
    upload covered, the fewer of them a mean would notice. These pairs are featureless, so the
    clock is the only signal available and the arithmetic is the whole test.
    """
    blank = MIN_FEATURES_TO_JUDGE - 1
    ride = Link(
        gap_seconds=300.0,
        inlier_count=0,
        match_count=0,
        features_before=blank,
        features_after=blank,
    )
    links = [*walk(3), ride, *walk(3), ride, *walk(3)]

    report = WalkthroughDetector().detect(links)

    assert report.cadence_seconds == 2.0
    assert [boundary.link_index for boundary in report.proposed] == [3, 7]
    assert [segment.frame_count for segment in report.segments] == [4, 4, 4]


def test_the_same_inlier_count_is_a_break_in_one_capture_and_not_in_another() -> None:
    """The bar is a fraction of what this walk's own links carry, not a fixed number.

    Fifteen verified matches is nothing in a capture whose steps share two hundred, and is an
    ordinary link in a dim capture whose steps share forty. Real imagery is what forced this:
    two structurally identical spaces with different surfaces still match each other by
    coincidence, well above any absolute floor low enough to be defensible.
    """
    rich = [link(inliers=200) for _ in range(11)]
    rich[5] = link(inliers=15)
    poor = [link(inliers=40) for _ in range(11)]
    poor[5] = link(inliers=15)

    assert len(WalkthroughDetector().detect(rich).proposed) == 1
    assert WalkthroughDetector().detect(poor).looks_like_one_walkthrough


def test_the_absolute_floor_still_applies_when_nothing_matches_anywhere() -> None:
    """A tenth of almost nothing is not a bar; below the solver's own minimum is."""
    barely = [link(inliers=30) for _ in range(11)]
    barely[5] = link(inliers=11)

    (boundary,) = WalkthroughDetector().detect(barely).proposed

    assert boundary.link_index == 5


def test_a_quick_turn_is_not_a_break_however_fast_the_cadence() -> None:
    """Video sampled twice a second makes three seconds six times cadence. It is still a turn."""
    fast = [link(gap=0.5) for _ in range(11)]
    fast[5] = link(gap=3.0)

    report = WalkthroughDetector().detect(fast)

    assert report.looks_like_one_walkthrough
    assert report.boundaries == []


# ---- slivers -----------------------------------------------------------------
def test_a_break_that_would_leave_a_sliver_is_reported_but_not_proposed() -> None:
    """Two frames are not a walkthrough, whatever happened between them and the rest."""
    links = [link(), link(inliers=0, matches=3), *walk(9)]

    report = WalkthroughDetector().detect(links)

    assert report.looks_like_one_walkthrough
    (considered,) = report.boundaries
    assert considered.scene_break is True, "the break itself is not in doubt"
    assert considered.proposed is False
    assert f"fewer than {MIN_SEGMENT_FRAMES} frames" in considered.reason


def test_a_sliver_at_the_end_is_demoted_too() -> None:
    links = [*walk(9), link(inliers=0, matches=3), link()]

    report = WalkthroughDetector().detect(links)

    assert report.looks_like_one_walkthrough
    assert report.boundaries[0].proposed is False


def test_demoting_the_last_break_does_not_demote_the_one_before_it() -> None:
    """The earlier break's segment grows when the later one is dropped, so it survives."""
    links = [*walk(5), link(inliers=0, matches=3), *walk(5), link(inliers=0, matches=3), link()]

    report = WalkthroughDetector().detect(links)

    proposed = [boundary.link_index for boundary in report.proposed]
    assert proposed == [5], f"only the tail break should have been dropped, got {proposed}"
    assert [segment.frame_count for segment in report.segments] == [6, 8]


# ---- the invariant the split depends on --------------------------------------
def test_segments_tile_the_frames_exactly() -> None:
    """Every frame in one walkthrough and none in two — what `split_session` insists on."""
    links = [*walk(4), link(gap=200.0, inliers=0, matches=2), *walk(6), link(inliers=0), *walk(4)]

    report = WalkthroughDetector().detect(links)

    covered: list[int] = []
    for segment in report.segments:
        covered.extend(range(segment.start_index, segment.end_index + 1))
    assert covered == list(range(len(links) + 1))
