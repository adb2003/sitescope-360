# Vision pipeline

What the computer vision in SiteScope 360 actually does, how well it does it, and — at least as
importantly — what it does not do.

This document is deliberately specific about limits. A construction team that trusts a wrong
capture path will walk to the wrong place; a mapper that is right most of the time and
confidently wrong occasionally is worse than no mapper at all.

---

## 1. The engine

```
VisionEngine                                  (app/vision/engine.py)
 ├── FeatureDetector    OrbFeatureDetector     ORB, equatorial band
 ├── FeatureMatcher     BruteForceMatcher      Hamming + Lowe ratio + spherical RANSAC
 ├── MotionEstimator    EssentialMotionEstimator  spherical essential matrix
 ├── PositionEstimator  DeadReckoningPositionEstimator
 ├── KeyframeSelector                          near-duplicate and quality rejection
 └── MapBuilder                                Umeyama similarity fit to a floor plan
```

Every component is injected through the constructor, so ORB-SLAM3, OpenVSLAM, COLMAP or OpenSfM
can replace the estimator without touching the pipeline stages or the API. The stages call only
methods on `VisionEngine`.

---

## 2. Image quality (§10) — *measured*

`app/vision/quality.py`. Classical measurements, not a learned model:

| Score | Method | Catches |
| --- | --- | --- |
| `blur_score` | Variance of the Laplacian, log-normalised | Walking blur, out-of-focus |
| `brightness_score` | Mean luma + shadow/highlight clipping fraction | Dark corridors, blown windows |
| `contrast_score` | Luma standard deviation | Flat, featureless frames |
| `resolution_score` | Pixel width against a construction-usability floor | Panoramas too small to read detail |
| `completeness_score` | Dead-area fraction + wrap-seam continuity | Failed stitches, partial panoramas |

`quality_score` is a weighted mean, but the **verdict is not**: any single catastrophic axis makes
a frame `unusable` regardless of the average. A perfectly exposed, perfectly sharp image of
nothing is still not a capture.

Calibration worth knowing:

- The resolution floor for a panorama is **1024 px wide**, and the target is 4096 px (≈11 px per
  degree — enough to read a rebar tag). A 1024-wide panorama is *deliberately* scored `poor`, and
  the warning names the recommended width. This surprised us in testing and is now an explicit
  test.
- Near-duplicates are detected with a 64-bit **dHash**. dHash compares adjacent pixel brightness
  rather than absolute values, so it survives an exposure change — which matters because the same
  corridor shot twice minutes apart must be recognised as a duplicate.

No claim is made that this matches any commercial vendor's proprietary quality algorithm. It
catches the failures that actually waste a site visit.

---

## 3. Feature detection and matching — *measured*

ORB, because it is free of patent restrictions, ships in the base OpenCV wheel, and is fast enough
to run on every frame of a long walkthrough.

**Detection is restricted to the equatorial band** (the middle ~55% of the image height). Features
near the poles of an equirectangular image are stretched by `1/cos(latitude)`, so their
descriptors are unstable and their bearing estimates noisy. On a construction site the walls,
columns and openings — the things worth matching — are around the horizon anyway.

Matching is Hamming brute force, then Lowe's ratio test at 0.75, then **geometric verification**.
That last step is not optional: scaffolding, formwork and ceiling tiles are visually repetitive,
and a ratio test alone accepts many wrong matches on them.

### Verification on the sphere

For calibrated bearing vectors the epipolar constraint is:

```
b₂ᵀ E b₁ = 0
```

This formulation needs **no pinhole model**, which is precisely why it works on a full 360 sphere
where a perspective homography or `cv2.findEssentialMat` would be invalid — points behind the
camera have no image-plane projection at all. So the essential matrix is estimated directly from
bearings with an 8-point solver under RANSAC (`verify_spherical`), followed by a refit on the
consensus set.

Measured on rendered ground truth: with half the correspondences replaced by random noise,
verification recovers ≥70 inliers with **under 5% contamination**.

---

## 4. Relative motion — *estimated*

`app/vision/motion.py` recovers the **rotation** and the **direction of translation** between two
panoramas by decomposing the spherical essential matrix, choosing among the four (R, t)
candidates by cheirality (which puts the most points in front of both cameras).

### It cannot recover distance

A single moving camera gives direction but not scale. That is a property of monocular geometry,
not a shortcoming of this implementation. Step length is therefore modelled as `pace × Δt` with a
single unknown pace, so the track's **shape** is meaningful and its **size** is arbitrary until
anchored to a floor plan.

### Pure rotation is detected and refused

If the operator turns in place, the essential matrix is identically zero and its SVD returns
numerical noise — a decomposition would happily produce a confident, meaningless translation.

So degeneracy is checked **first**, with an orthogonal Procrustes rotation-only fit:

```
minimise Σ ‖R·b₁ᵢ − b₂ᵢ‖²   over rotations R
```

The median angular residual after removing the best rotation *is* the parallax. Below 0.35° the
platform reports rotation with **zero translation** and says why. (An earlier version decomposed
first and inspected the result afterwards — judging degeneracy with the very numbers the
degeneracy had corrupted. The ground-truth test caught it.)

### Confidence

The per-pair confidence is the **geometric mean** of four terms — inlier ratio, inlier count,
parallax, and cheirality agreement. A geometric mean, not an arithmetic one: every term must hold
up, and one weak axis should not be averaged away.

---

## 5. Position estimation — *estimated*

`app/vision/position.py` dead-reckons a floor-plane track from consecutive motions. Two
assumptions, stated because they are the reason the output is labelled *estimated*:

1. **Scale** — constant pace × elapsed time (see above).
2. **Planarity** — only the horizontal (X, Z) components are integrated. On a single floor that
   holds well; on a ramp or a stair it does not.

Confidence decays **multiplicatively** along the chain, which is the honest model of dead
reckoning: one bad link poisons everything downstream of it. A link below 15% confidence *breaks*
the chain rather than contaminating it — the position is held, the break is recorded, and the
chain confidence collapses.

### Frame spacing is the dominant factor

Measured on rendered panoramas of the same room, verified correspondences after RANSAC:

| Frame spacing | Verified inliers | Usable? |
| --- | --- | --- |
| 0.5 m | ~363 | Comfortably |
| 1.0 m | ~229 | Yes |
| 2.0 m | ~125 | Yes |
| 4.0 m | ~25 | **No** — below what the solver needs |

An operator walking at roughly 1 m/s with a frame every 1–2 s lands squarely in the usable range.
This is why `DEFAULT_STEP_M` is 1.5 m and why the default video frame interval is 2 s.

### Measured accuracy

Accuracy is only meaningful *after* the similarity fit, because the estimator is scale-free by
construction — and that is exactly how the platform uses it.

Two separate measurements, because they answer different questions.

**The geometry, on exact correspondences.** 3D points projected into two known camera poses, with
no feature detector involved (`test_track_integration_is_exact_on_synthetic_correspondences`):

```
max track error   < 0.05 m over a 5-pose L-shaped walk
recovered scale   1.00 ± 0.05
per-pair yaw      within 1° of truth
translation dir.  cos > 0.99 against truth
```

That is the maths itself, and it is portable.

**End to end through ORB, and this one is platform-dependent.** Same 21-frame walk through the
20 × 12 m room at 1.5 m spacing (`test_track_through_orb_is_accurate_enough_to_place_points`):

| Platform | mean | median | max | recovered scale |
| --- | --- | --- | --- | --- |
| Windows (dev machine) | 0.13 m | 0.14 m | 0.29 m | 1.011 |
| Linux (GitHub Actions) | 1.57 m | — | ~3.6 m | — |

**Identical library versions on both** (opencv-python-headless 5.0.0.93, numpy 2.5.3). The
difference comes from the platform builds of OpenCV: ORB's FAST corner detection and its scale
pyramid are compiled with different SIMD paths, so the keypoints differ slightly, the
correspondence set differs, and the linear essential-matrix estimate moves with it. The error
profile on Linux is large at both ends of the track and small in the middle — the signature of a
systematic shape error propagated by dead reckoning, not of random noise.

This was found by CI, not by reasoning, and it is why the two tests are split. An earlier single
test asserted `mean < 1.0 m`, which passed on the development machine and failed in CI — a bound
tuned to one machine masquerading as a property of the algorithm.

The end-to-end test now asserts the weakest property that still makes the feature worth shipping:
mean error under **10% of the room diagonal** (2.33 m here), enough to identify which bay or wing
a capture came from after anchoring. Both platforms clear it — Windows at 0.6%, Linux at 6.7%. The
measured figure is printed into the CI log on every run rather than only a pass/fail.

**What this means in practice.** Do not quote a single accuracy number for this estimator. It is
accurate enough to place capture points for review, it is labelled `estimated` everywhere it
surfaces, and it asks for manual placement when confidence is low — which is the design that makes
the platform-dependence tolerable rather than dangerous.

The same test file asserts the opposite property too: at 6 m spacing the mapper **must** report
low confidence and demand manual placement.

### Ambiguity in the four-fold decomposition

`_decompose` chooses among four (R, t) candidates by cheirality — which puts the most points in
front of both cameras. It also returns the **margin** over the runner-up, and a pair whose margin
falls below `MIN_AMBIGUITY_MARGIN` (0.25) is **refused**: zero confidence, no translation
published, the reason recorded.

The reason it is a refusal and not a penalty: the four candidates differ by a sign on the
translation, so when two score almost equally the direction of travel is undetermined by the data
and numerical noise decides it. A flipped `t` is not slightly wrong, it is roughly 180° wrong, and
dead reckoning carries that through every later frame. "Somewhat less confident" is not what that
situation is.

This was got wrong on the first attempt, and the fix is worth recording because the failure was
silent. The margin started as a fifth term in the confidence geometric mean. A test then measured
what a coin-flip pair actually scored: **0.60** — above `MAPPING_CONFIDENCE_THRESHOLD` (0.45), so
it would have been published as an automatic path. A fifth root cannot express a disqualifier; it
dilutes one. Graded terms are for quantities that degrade smoothly, and this one does not.

Honesty about what the margin did *not* fix: it was added while investigating the platform
discrepancy above, on the hypothesis that a near-tie was flipping between platforms. Measurement
did **not** support that hypothesis — every frame pair in the demo walkthrough has a margin of
0.86–1.00, so none are near-tied, and it does not explain the Windows/Linux difference. It is kept
because it guards a real failure mode, not because it resolved that one.

---

## 6. Publishing a result — the §46 gate

`summarize_mapping` decides what the platform is willing to show. The published confidence is the
**median step confidence** — not the mean, not the best. A walkthrough is only as mappable as its
typical link.

If that median falls below `MAPPING_CONFIDENCE_THRESHOLD` (default 0.45, configurable):

```
Automatic mapping confidence: Low (37%) — manual placement recommended.
1 of 5 frame pairs could not be related geometrically.
```

`requires_manual_placement` becomes true, no automatic path is drawn, and the interface renders
that message **verbatim**. The UI does not paraphrase it: restating a refusal in friendlier words
is exactly how "low confidence" turns into something a site team reads as an answer.

An estimated path that *is* published is drawn **dashed** on the floor plan; a manually placed one
is solid.

---

## 7. Anchoring to a floor plan

`app/vision/mapbuilder.py`. The track's shape is meaningful but its position, rotation and scale
are arbitrary, so anchoring is a **similarity transform** (uniform scale + rotation + translation)
fitted in closed form with the Umeyama estimator to two or more user-supplied anchors.

Deliberately *not* affine or projective: allowing shear or perspective would let a single bad
anchor silently distort the geometry into something that looks plausible and is wrong. Reflection
is also forbidden — a mirrored walkthrough is never the right answer.

Refusals:

- Fewer than two anchors → scale and rotation are undetermined.
- Collinear or coincident anchors → the fit is refused with an explanation.
- Residual above 60 px → reported as a poor fit, and manual placement is recommended.

---

## 8. What is not implemented

`app/vision/ai.py` declares these as interfaces and they raise `NotImplementedError` rather than
returning plausible numbers. `GET /api/system/capabilities` reports them as unavailable, and the
UI reads that endpoint rather than hard-coding assumptions.

| Capability | Status |
| --- | --- |
| Object detection | Not implemented — Phase 3 |
| Defect detection | Not implemented — Phase 3. Defects are recorded manually as annotations. |
| Progress detection | Not implemented — Phase 3. Needs a trained model *and* capture history. |
| BIM comparison | Not implemented — Phase 3. Interfaces in `app/bim/`. |
| Loop closure / global optimisation | Implemented — `vision/slam.py`. SE(2) pose graph, Levenberg-Marquardt, Huber kernel. See below. |
| Bundle adjustment over 3D landmarks | Not implemented. The graph optimises poses; there is no landmark map. |
| Metric positioning | Not possible monocularly. Direction only — see *Scale*, below. |

### Loop closure and short-range constraints

The graph carries three kinds of edge:

- **Odometry**, between consecutive frames, taken from the initial dead-reckoned track — so they
  are satisfied exactly and contribute no disagreement for the solver to distribute.
- **Loop closures**, between frames at least `MIN_LOOP_SEPARATION` apart that a perceptual-hash
  shortlist and then geometric verification agree show the same place.
- **Short-range constraints**, between frames 2 and 3 apart. Not revisits — just the same
  bearing-and-heading observation over a short baseline. They exist because everything between
  "consecutive" and "eight apart" used to constrain nothing at all, while those images still
  overlap heavily.

Measured over six rendered walkthroughs against ground truth, the full pipeline lands a median
33% closer than raw odometry and is better on four of six. The short-range edges are worth about
half of that (median 11% against the closure-only pipeline), and they are the *entire* gain on
walks that never double back, which loop closure alone cannot help at all.

One of the six came out **worse** than plain odometry — a false closure that the Huber kernel and
residual rejection reduced without removing. That is reported rather than averaged away: the
technique is not uniformly better, and `loop_closure.limitations` in `describe_capabilities()`
says so.

### Frame sampling

One frame per second of video, and it is a measured figure rather than a convention. A person
walks about 1.2 m/s, so the interval sets the distance between consecutive frames, and that
distance is what decides whether ORB can match them at all:

| interval | step | verified matches/pair | mean track error (3 routes) |
| --- | --- | --- | --- |
| 2.0 s | 2.4 m | ~38 | **3.64 m** |
| 1.5 s | 1.8 m | ~60 | 0.66 m |
| 1.0 s | 1.2 m | ~114 | **0.62 m** |
| 0.75 s | 0.9 m | ~157 | 2.87 m (single route) |
| 0.5 s | 0.6 m | ~234 | 2.68 m (single route) |

2.0 s — the previous default — was the worse choice on every route tested, and on two of the
three it produced **no loop closures at all**, because frames 2.4 m apart do not match well
enough to verify a revisit. Denser is not automatically better either: below about 1 s the chain
grows faster than the per-step geometry improves, and the error climbs again.

Stretches where the operator stood still are dropped during extraction rather than stored and
flagged later. At a one-second interval a thirty-second pause is thirty near-identical panoramas,
each costing three stored objects and a quality assessment, and the mapper discards them as
duplicates anyway. This is safe for video specifically because the source file is kept —
re-processing at a different interval reconstructs anything skipped. Uploaded stills are never
dropped: each one is a picture somebody deliberately took.

### Scale

What none of this recovers is *distance*. A single moving camera gives translation only up to an
unknown factor, so step length is modelled as `pace × Δt` with one assumed pace. Three things
were measured and rejected as ways around it:

| Attempt | Result |
| --- | --- |
| Higher resolution / more ORB features | No effect. 87 → 476 inliers changed mean error by nothing; 4096×2048 was *worse* and 16× slower. |
| Step length from each pair's median triangulated depth | Worse on two of three walks (+528%, +74%, −30%). Scene depth is not constant enough. |
| Three-view scale propagation over shared points | Not viable: at realistic 1–2 m steps only 3–4 keypoints survive matching in both pairs. |

The honest consequence is that a walk at varying speed is less accurate than one at constant
speed — measured at 0.13 m vs 0.48 m mean error on the same route — and no amount of image
processing fixes it. Metric scale needs a second sensor: an IMU on the capture device, which is
what makes "VIO" visual-*inertial* rather than merely visual.

`HistoricalProgressDetector.compare()` returns an `UnavailableAnalysis` naming exactly what is
missing, rather than a percentage.

The one honest change signal the platform *can* offer between two dates is
`VisionEngine.similarity()` — the verified inlier ratio between two frames at the same location.
It is labelled a **similarity measurement**, not a progress percentage.

---

## 9. Demo and test imagery

`app/processing/synthetic.py` renders equirectangular panoramas by ray-casting a 3D room
(ray/box for the shell, ray/cylinder for structural columns, blocky value noise for texture).

Generated rather than downloaded for two reasons. The licensing question disappears — every pixel
is produced by that file. And, more usefully, the panoramas have **real geometry**: because they
are cast from known camera positions, consecutive frames contain true parallax, so the matcher,
the essential-matrix solver and the position estimator operate on genuine multi-view geometry and
their output can be checked against the camera path that produced it. Two unrelated stock
panoramas would only ever exercise the code paths, never the correctness.

The projection convention matches `app/vision/panorama.py` exactly — and a frontend test
(`geometry.test.ts`) asserts the TypeScript viewer agrees with the Python backend term by term,
because a flipped sign there would misplace every annotation pin and look like a rendering bug.

---

## 10. Performance

Measured locally on the demo dataset (2048 × 1024 panoramas):

| Stage | Cost |
| --- | --- |
| Validate | ~0.2 s per file |
| Metadata | ~0.02 s per file |
| Frame extraction (stills) | ~0.08 s per frame incl. thumbnail + preview |
| Quality | ~0.14 s per frame |
| Features | ~0.19 s per frame |
| Mapping | ~0.08 s per frame pair |

Feature detection and matching are analysed at **2048 px width** regardless of source resolution
(`MAPPING_ANALYSIS_WIDTH`). Matching quality saturates well below native resolution, so this is
roughly a 10× saving on an 8k panorama with no measurable accuracy loss.

Video frames are extracted by **sequential decode with a modulo test**, not by seeking. Seeking
with `CAP_PROP_POS_FRAMES` on a long-GOP 360 video is both slow (it re-decodes from the previous
keyframe anyway) and unreliable across containers. One frame is live in memory at a time.
