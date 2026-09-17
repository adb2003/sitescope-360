# Roadmap

Three phases (§41). Phase 1 is what this repository contains and what has been verified; Phases 2
and 3 are the intended direction, with the seams that already exist for them.

---

## Phase 1 — MVP · **delivered**

Everything in the §52 Definition of Done, verified end to end through HTTP by
`backend/tests/integration/test_definition_of_done.py`.

| Capability | Status | Notes |
| --- | --- | --- |
| Project management | ✅ | Project → Building → Floor → Zone, with declared areas and expected capture points. |
| Authentication and RBAC | ✅ | JWT + rotating refresh tokens; four roles enforced at the route. |
| Mobile capture workflow | ✅ web · ⚠ native | The responsive web flow is verified. The Expo app is complete and syntax-checked but not executed (no mobile toolchain here). |
| Local upload | ✅ | Streaming, magic-byte sniffed, size-capped, checksummed, de-duplicated. |
| 360 image viewer | ✅ | Three.js: drag, wheel/pinch zoom, keyboard, fullscreen, hotspot navigation, annotation pins. |
| Video frame extraction | ✅ | Configurable interval; sequential decode, one frame in memory at a time. |
| Floor plans | ✅ | Upload, revisions, two-point calibration, zoom/pan editor. |
| Manual capture locations | ✅ | Add, drag, delete, bind frames; pixel-space coordinates. |
| Capture path | ✅ | Ordered by timestamp; dashed when estimated, solid when manual. |
| Timeline | ✅ | Activity by date with the visited-point share and its denominator stated. |
| Date comparison | ✅ | Side-by-side and slider wipe, orientation-synchronised. |
| Capture frequency | ✅ | Configurable cadence, grace period, due/overdue/never-captured. |
| Coverage | ✅ | Declared-point ratio with apportioned area, provenance and method stated. |
| Annotations | ✅ | Spherical-degree pins, priority, resolution workflow. |
| Dashboard | ✅ | Portfolio and per-project, with an explicit empty state. |
| Reports | ✅ | 4 types × 3 formats, with a provenance section that survives CSV export. |
| Processing pipeline | ✅ | 7 independent, idempotent, retryable stages on a pluggable queue. |
| Observability | ✅ | Structured JSON events, correlation ids, admin job dashboard. |
| Docker / CI | ⚠ written | Compose, Dockerfiles, nginx and a 6-job CI workflow exist but were not executed locally (no Docker here). CI runs them. |

### Beyond the brief's Phase 1

Some Phase 2 work landed early because it was needed to make Phase 1 honest:

- **Relative visual positioning** — a working two-view spherical essential-matrix estimator.
  The geometry recovers a known pose to centimetres; end to end through ORB the track error is
  platform-dependent (0.13 m on Windows, 1.57 m on Linux CI) and documented as such.
- **Automatic mapping refusal** — the mapper declines when confidence is low, which is what makes
  the feature safe to ship at all.
- **Feature matching and image similarity** — verified-inlier similarity between two dates at the
  same location.
- **Keyframe selection** — near-duplicate and low-quality rejection before mapping.

---

## Phase 2 — Intelligent mapping

Making automatic placement good enough to trust without anchors.

### 2.1 Loop closure and global optimisation · **delivered**

**The problem.** Dead reckoning drifts. Confidence decays multiplicatively along the chain, so a
long walkthrough ends with a low-confidence tail even when every individual step was good.

**What was built.** `app/vision/slam.py` — revisit detection over dHash perceptual hashes and
ORB, then SE(2) pose-graph optimisation by Levenberg–Marquardt with a Huber robust kernel and
bearing-only residuals. Co-located (zero-parallax) closures are handled separately from ones with
a baseline, and short-range **skip constraints** across 2–3 frame spans mean a corridor walk that
never revisits is still globally optimised rather than left as a dead-reckoning chain.
`mapping_method` on the session records which path produced the track.

**What is still open here.** Place recognition is within a session only, so it cannot match
against a floor captured last week — that is 2.2 below, and it is the harder half. A custom
solver was written rather than adopting g2o or GTSAM: both are C++ builds with heavier licence
and packaging consequences than a few hundred lines of NumPy, and the graph sizes here (tens to
a few hundred poses) do not need their performance.

### 2.2 Cross-session localisation

Place a *new* capture against an *existing*, already-anchored floor. This is the highest-value
Phase 2 item: it removes manual placement entirely for a floor captured before.

The data is already there — features are persisted per frame as `.npz` blobs, and locations are
stable across visits — so this is a matching-and-solving problem, not a schema problem.

### 2.3 Automatic capture grouping

Split one upload into logical walkthroughs (per floor, per zone) using time gaps and visual
discontinuity, so an operator can upload a whole day at once.

### 2.4 Scale from a second signal

Monocular geometry cannot recover scale. Options, in order of practicality:

1. **Floor-plan calibration** — already stored (`px_per_meter`); with two anchors the scale is
   already exact. This is the current answer.
2. **IMU / step count** from the phone during capture — the mobile app already runs during the
   walk.
3. **Known object size** in frame (a door leaf, a standard block course).
4. **Stereo or depth** from a camera that provides it.

### 2.5 Better quality detection

The current engine is classical and honest. A learned model could add: obstruction detection (a
hand or a bag over the lens), motion-blur direction, and stitch-artefact classification. It must
report `ai_predicted` provenance, not `measured`.

### 2.6 Substituting a real SLAM system

Every vision component is injected, so ORB-SLAM3, OpenVSLAM, COLMAP or OpenSfM can replace the
estimator without touching the pipeline stages or the API. The realistic path is offline
Structure-from-Motion (COLMAP/OpenSfM) as a *second*, slower mapping stage that supersedes the
fast online estimate when it finishes — the `ProcessingJob` model already supports adding a stage.

---

## Phase 3 — Construction intelligence

Everything here is currently **declared and unimplemented**, reported as unavailable by
`GET /api/system/capabilities`. Nothing fabricates a number.

### 3.1 BIM import and comparison

Interfaces exist in `app/bim/interfaces.py`: `BimSource`, `BimElement`, `BimComparator`,
`CoordinateAlignment`, `Deviation`. They raise `BimNotImplementedError`.

```
Revit / IFC → BIM processing → element graph → shared coordinate system
                                                      ↓
                    deviation report ← computer vision ← actual site 360
```

**Already in the schema:** `floors.elevation_m`, `floor_plans.px_per_meter`,
`floor_plans.calibration`, `capture_locations.x/y`, `capture_frames.heading_deg`, and
`DataProvenance` on every positional field.

**Still required, and not pretended away:** a global site coordinate system (project base point /
survey point), per-floor registration between drawing space and model space, and element
*geometry* rather than element identity.

First implementation would use `ifcopenshell` (LGPL — note the licence implication) for IFC, which
is the open standard; Revit-native import needs Autodesk tooling.

### 3.2 Progress detection

`HistoricalProgressDetector` returns an `UnavailableAnalysis` naming what is missing: a trained
model, at least three historical captures at the location, and a BIM or schedule reference for
expected state.

The honest intermediate step already available is `VisionEngine.similarity()` — a *similarity
measurement* between two dates at the same location, explicitly not a progress percentage.

### 3.3 Defect detection

Not implemented. Defects are recorded manually as annotations, which is exactly the training set a
future model would need — annotations already store frame, spherical position, priority, status
and resolution.

### 3.4 Object and material detection

Not implemented. `ObjectDetector` and `DefectDetector` protocols exist and raise. A detector would
return `Detection` objects with `DataProvenance.AI_PREDICTED`, so the UI renders them as
predictions from day one.

### 3.5 Schedule comparison

Needs 3.1 (BIM elements carry `scheduled_start` / `scheduled_finish` in the declared type) plus
3.2. Output would be per-element status against plan, with provenance.

### 3.6 Safety detection

PPE, guarding, egress obstruction. Same shape as 3.3, and the same requirement: an annotation-like
record with `ai_predicted` provenance, reviewable by a person.

### 3.7 Risk and project intelligence

Aggregate signals across projects. The prerequisite is that the inputs are trustworthy, which is
why Phases 1 and 2 come first — a risk score built on a mapper that quietly guesses is worse than
no score.

---

## Deliberately out of scope

Not oversights:

| | Why |
| --- | --- |
| Robot control | The `CaptureSource` and `CameraProvider.trigger_capture()` seams exist (§47). Driving hardware is a different product. |
| Real-time streaming from a camera | The MVP path is file-based on purpose; live transfer is a per-vendor adapter concern. |
| In-browser panorama stitching | Cameras and their desktop tools already do this well. |
| On-device processing | Frame extraction and feature detection are CPU-bound and belong on the server; the phone uploads and reports. |
| Multi-tenant billing | An organisational concern, not a monitoring one. |

---

## Known limitations today

Collected in one place, all documented at their source:

1. **Positioning is relative and scale-free.** Anchor two points on a floor plan to place a track.
2. **Graph SLAM, but monocular.** Loop closure and SE(2) pose-graph optimisation are implemented
   (`app/vision/slam.py`: Levenberg–Marquardt with a Huber kernel, bearing-only residuals, plus
   short-range skip constraints so a walk that never revisits still gets optimised). Drift is
   bounded rather than eliminated: a route with no revisit has nothing to close against, and
   `mapping_method` records which of the two produced each track. Scale and bearing are still
   absent — see (1).
3. **Planar motion assumed.** Ramps and stairs violate it.
4. **Frame spacing matters a lot.** Below ~2 m works; at 4 m the mapper correctly refuses.
5. **Track accuracy through ORB is platform-dependent** — 0.13 m mean on Windows vs 1.57 m on
   Linux CI with identical library versions, because platform builds of OpenCV detect slightly
   different keypoints. Quote a range, not a single number. Removing the variance would need a
   pinned, reproducible feature detector.
6. **Coverage is declared, not detected.** It compares visited points to administrator-declared
   expectations; square footage is apportioned, never measured from imagery.
7. **One PDF sheet becomes one plan.** A sheet set is rasterised a page at a time, chosen on
   upload — a 40-page set also contains sections, elevations and details, and importing them all
   as floor plans would be worse than importing none. There is no sheet picker that previews the
   pages; the operator has to know which page they want.
8. **The compass is relative**, not magnetic — an uploaded image carries no device heading, and the
   viewer labels it `rel`.
9. **The thread queue is not durable.** Fine for development; `QUEUE_BACKEND=redis` for anything
   real. The API warns at startup and the API reports `"durable": false`.
10. **Two adapters are unverified against hardware** (OSC, Insta360) and report
   `implemented: false`.
11. **No progress, defect, object or BIM analysis.** Phase 3.
