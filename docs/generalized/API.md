# MotionMuse schema 3 / generalized plane API

## Coordinates and configurations

Detection and fitting always use original, unmirrored camera pixel coordinates.
`landmarks_px` remains in the display frame, including the existing mirror policy;
all original 21×3 normalized/world arrays and anatomical handedness remain unchanged.
Conversion back to raw is `x_raw = width - 1 - x_display` when mirrored.
UV is an unclipped projective 2D coordinate, not hand-to-board distance or contact.

`BoardDefinition` JSON schema_version **1**: `board_id`, `dictionary`, `markers`
keyed by distinct dictionary IDs, each with four finite `corners_uv`. Corner order
is OpenCV's decoded marker orientation TL/TR/BR/BL, NOT image-axis sorting.
Rotated markers retain decoded order. Optional `outline_uv` and `metadata` never
provide fitting correspondences. 1–12 markers are tested; dictionary size is the
upper bound (50 by default). Single-marker definitions load but cannot produce valid.
Overlapping markers, self intersections, unknown dictionary, duplicate JSON keys,
out-of-range IDs and numerically degenerate configurations fail at load.

Legacy `InstrumentPreset` JSON schema_version **1**: `preset_id`, `chords` mapping supported
C/G/Am/F names to simple polygons, `strum` polygon, `axis` directed endpoints,
optional `interaction` overrides for InteractionConfig. No marker IDs or board ID.
Concave regions and coordinates outside the unit square are supported. Boundary
margin uses signed polygon distance; overlapping regions use insertion order.
Directed-line normal is (-dy, dx), normalized in UV. Positive crossing is down.
The `flip-u` compatibility transform reflects regions and this normal together.

Optional `chord_frame` and `strum_frame` are convex four-corner polygons in plane
UV, ordered as local (0,0)/(1,0)/(1,1)/(0,1). When present, chord margins and strum
distance/speed/jump thresholds use the corresponding region-local unit square.
Published landmarks and display polygons remain in board UV. Legacy presets
without these fields retain their original UV units. The compatibility field
`strum_speed_uv_s` uses region units/s when `strum_frame` is supplied.

New presets use **schema_version 2** with `preset_id`, exactly seven `degree_regions`
(string keys `"1"` through `"7"`), required convex `selection_frame`, `strum`, `axis`,
optional `strum_frame` and `interaction`. Subdivision follows the selection frame's
local horizontal coordinate. No musical names or tonic are stored in geometry.
Live loading upgrades v1 presets with a `chord_frame`, and the known default four-cell
layout, in memory only. Unrecoverable custom layouts remain visible but disable
interaction with `layout_requires_calibration`; redraw the two regions. Historical
recordings never undergo this live upgrade.

## Performance settings and musical events

GET `/api/performance` returns `mode`, `tonic`, `revision`, `service_instance_id`, `tonics` and seven
`voices` (`degree`, `label`, `chord`, `notes_midi`). POST the complete object
`{"mode":"melody","tonic":"D"}` to update. Modes: `chord`, `melody`. Tonics:
`C, Db, D, Eb, E, F, F#, G, Ab, A, Bb, B` (ASCII wire spelling). Invalid bodies and
changes during calibration return 400. GET and POST return 409 while the worker
is unavailable.
Controls use the existing same-origin and loopback restrictions. No-op updates
retain revision. Real changes increment it; settings live until service restart.

Native snapshots include `performance`, `layout.degree_regions`, and a top-level
`service_instance_id`. Snapshot settings belong to the same immutable frame/JPEG.
Following a POST, wait for a live frame with the returned revision or newer for
that service instance before drawing the new settings. Each restart generates a
new instance ID and restores chord/C and revision 0. Clients use the server's
spelled voice labels.

Interaction adds `selected_degree`, `selected_label`, `selection_valid`,
`candidate_degree`, `candidate_label`, `selection_point_uv`, `selection_reason`,
`selection_dwell_progress`. Legacy `chord_*` diagnostic fields remain; chord names
are null and `chord_valid` is false in melody mode. `selection_valid` gates both modes.

Events retain ID/frame/time/direction/velocity and add `mode`, `tonic`, `degree`,
`label`, `notes_midi`, `duration_ms` (900), `performance_revision`. `chord` is the
actual chord name in chord mode, null in melody mode. Musical mapping is frozen
at emission. Audio rejects obsolete revisions, drains old pending events and
fades active voices over 10 ms after changes. Event IDs never reset on a change.
Re-selection requires a fresh dwell and new strum-side observation. Sample banks
are preloaded; event submission performs no synthesis, disk I/O or blocking playback.

Recording configurations include `performance` (mode/tonic/revision). Replay restores
it at frame boundaries and does not reconstruct the plane tracker for music-only
changes. Missing performance data means historical chord/C semantics and original
four-chord voicings. Old v1 geometry remains unmodified during replay.
Replay preserves pauses recorded as `calibration_in_progress` or
`layout_requires_calibration`.

## Fingertip selection and strum trajectories

Selection tests the left index fingertip (landmark 8) against polygon interiors,
with a 100 ms dwell. The display fills all seven cells and highlights the selected
interior. `selection_margin_fraction` defaults to .05: for schema-2 presets the
entry/hysteresis margin is capped at this fraction of the cell's shortest bounding
extent in selection coordinates. `null` retains the historical `chord_margin`.

`strum_path_enabled` defaults to true. Successive reliable right-index observations
must intersect the finite strum axis inside its polygon and reach opposite sides
of the .45/.55 hysteresis thresholds. Intermediate observations in the central
buffer retain a real crossing; an unobserved shortcut between distant anchors is
never substituted. `strum_tracking_margin` (.35) expands the observation zone in
strum-local coordinates, allowing both samples to be just outside the polygon.
The per-frame .45 jump cap is waived only for an observed axis-crossing segment;
the 12 units/s speed ceiling, identity, freshness and board-valid checks remain.
Minimum normal speed is .6 units/s; event cooldown is .18 s.

Interaction also publishes `strum_point_uv` (board coordinates),
`strum_point_local` (strum coordinates), `strum_in_region`, and `strum_reason`.
The web readout distinguishes selection not ready, missing/unreliable right hand,
outside zone, waiting for a starting side/crossing, missed axis, motion outlier,
too slow, cooldown and successful trigger. The overlay marks this point `STRUM`.
New recordings save all interaction options. Replay defaults missing
`strum_path_enabled` to false and missing `selection_margin_fraction` to null,
so recordings from before this change retain their original interaction behavior.

## HTTP compatibility and projection semantics

| Endpoint | Contract |
|---|---|
| GET `/api/landmarks`, `/api/state` | Explicit schema **2** adapter; old paths preserved |
| GET `/api/v3/landmarks`, `/api/v3/state` | Native schema **3**, recommended for new consumers |
| GET `/api/snapshot` | `{packet: <schema 3>, image_base64: <JPEG or null>}`, same immutable frame |
| GET `/frame.jpg` | Existing JPEG with X-Frame-Id |
| POST `/api/record/start` | Optional `{"raw_frames":true}`; omitted uses CLI default |
| POST `/api/record/stop`, `/api/shutdown` | Existing local controls |

Native `hand.projection_status` is valid/degraded/held/unavailable.
`projection_available` means 21 points can mathematically be projected;
`interaction_eligible` additionally requires current board status valid.
`landmarks_instrument_uv` may be populated while held/degraded for display/debug.
Only valid enters InstrumentLogic; all other states clear chord dwell and strum
history. A valid recovery must rearm. Audio retains existing once-only event IDs.

The schema 2 adapter retains the old mathematical meaning of projection_status:
valid if UV is available, otherwise unavailable. It maps degraded board status to
held (never valid). New `pose_quality.status` retains the actual state. Consumers
of v2 must continue gating on instrument.status; migrate to v3 for explicit safety.
No silent schema 2 reinterpretation. `/api/snapshot` is upgraded to v3 because
it is consumed by the bundled updated UI. New JSONL recordings also use v3.

Python compatibility: `ArucoBoardTracker()` preserves strict old four-center
semantics. The pipeline/application default uses `learn_legacy=True`: on the first
valid 0/1/2/3 frame it learns all encoded corners, then uses joint corner tracking.
Supplying a BoardDefinition always selects joint corner tracking.

## Localization quality

`instrument` retains homography_camera_to_instrument, its inverse, visible_ids,
required_ids (now registered IDs, not an all-must-be-visible requirement), raw
marker corners/centers, source_frame_id, source_timestamp_monotonic_s and aruco_ms.
Adds board_id, tracking_mode, outline_uv and pose_quality:

| Field | Meaning |
|---|---|
| status / reason | valid, degraded, held, lost / machine-readable cause |
| visible_marker_count | Distinct detected registered IDs (unknown IDs excluded) |
| correspondence_count | Usable submitted corner pairs, before RANSAC rejection |
| ransac_inlier_count | Raw corner-level RANSAC inliers |
| inlier_count / inlier_ratio | Final complete-marker inliers / submitted pairs |
| inlier_marker_count | Markers with all four supported corners |
| reprojection_rmse_px | Pixel RMSE after complete-marker refit; null when unavailable |
| coverage_ratio | UV inlier hull area / hull of registered markers, unit reference and optional outline |
| spread_ratio | Square root of smaller/larger UV covariance eigenvalue |
| normalized_condition | Homography condition after independently centering/scaling both point sets |
| temporal_jump_diagonal | Largest reference-point movement divided by frame diagonal, when a prior pose exists |
| age_s | Current estimate: 0; held: elapsed since last valid; unavailable: null or expired age |

Fit UV → raw pixels with `findHomography(RANSAC, 3 px)` and invert the result,
so residual thresholds have pixel units. Partial marker inliers are rejected as
groups and the complete supported markers are refit jointly. Unknown IDs never
enter correspondences. Duplicate registered IDs/nonfinite registered detections
clear the trusted cache immediately. Other failed estimates may display held only
until the configured expiration, measured from the last valid pose.

Default valid requires ≥2 full inlier markers, ratio ≥.8, RMSE ≤2 px, coverage ≥.10,
spread ≥.035, marker edges ≥8 px, support pixel hull ≥400 px², normalized condition
≤1000 and consistent projective denominators over the support domain. Implausible
movement above `.18 + .8*min(dt,.25)` frame diagonals is degraded; recovery requires
at least three consistent candidate frames spanning .10 s. One-marker perfect
fit is still degraded (`single_marker_extrapolation`). These thresholds are
conservative heuristics, not calibrated probabilities or proof of 3D planarity.

`inference_ms` remains hand-model time; `aruco_ms` remains detection + plane fitting;
`vision_total_ms` remains hands + ArUco + coordinate fusion (before logic/audio/drawing).
Replay reusing hand nodes retains recorded inference_ms and labels replay_hands;
its vision_total_ms measures only work actually rerun.

## Frozen-frame calibration controls

All POST controls share existing loopback/explicit-remote-control and Origin /
Sec-Fetch-Site protections. Bodies are JSON, calibration limit 16 KiB. Errors
are UTF-8 HTTP 400; I/O failures HTTP 500; unauthorized control HTTP 403.

1. `POST /api/calibration/capture {}` → session_id, source frame_id, frame_width,
   frame_height, mirrored, visible_ids, image_base64, expires_in_s and
   suggested_board_id. The suggestion is available in both boards and presets
   at the time of capture. Copies raw image and same-packet detections atomically
   under the pipeline lock. Only one frozen session exists; new capture
   invalidates the previous ID. TTL 600 s.
   At least two distinct sufficiently large/contrasty/sharp markers are required.
2. Browser maps canvas pointer positions through object-fit containment and
   inverse reflection to original raw pixels. Letterbox clicks are ignored.
   The UI sends `POST /api/calibration/preview {session_id, board_id,
   regions_raw:{chord:[[x,y],...], strum:[[x,y],...]}}`. Each region has four
   ordered corners defining its playing direction, including reflected winding
   on a mirrored canvas. Rectangles can be dragged in any direction; handles
   and four-point selection support perspective quadrilaterals. Both regions
   must lie on the same rigid plane, must not overlap, and must pass minimum
   area/edge/convexity checks. The chord quadrilateral establishes a positively
   oriented board frame; its seven selection cells and the strum center line are
   stored separately in a generated preset with local coordinate frames.
   The board support outline includes both selected regions for pose checks.
   Response: board, preset, pose_quality, backend-rendered preview JPEG and
   preview_token. Invalid previews invalidate previous tokens.
   Legacy callers may instead send `points_raw:[[x,y],...]` in physical
   TL/TR/BR/BL order; this retains the existing layout and returns preset=null.
   Supplying both reference points and regions is rejected.
3. `POST /api/calibration/save {session_id, preview_token}` atomically publishes
   each file without overwrite, returns saved/preset_saved/board_id/active.
   New region selections write `config/presets/<board_id>.json` first and
   `config/boards/<board_id>.json` last. A board publication failure rolls back
   the newly created preset; existing files are never overwritten. An abrupt
   process termination between publications can leave an unreferenced preset,
   but a published board always has its companion. Board metadata stores the
   safe `calibration_preset` name. `--board` resolves it from the sibling
   `presets/` directory unless an explicit `--preset` is supplied. Keep both
   files when copying a calibration. Legacy saves return preset_saved=null.
   The worker switches board, layout and interaction config at a frame boundary,
   clears action history, and preserves the monotonic audio event ID sequence.
   New manual selections use exactly the drawn positions (flip_u=false).
4. `POST /api/calibration/cancel {session_id}` releases the frozen frame; current
   board remains. Edits invalidate the browser preview token until another preview.

No webpage parameter can supply a directory or arbitrary filename. Names are
ASCII [A-Za-z0-9][A-Za-z0-9_-]{0,63}; Windows reserved names are rejected. File
publication uses an fsynced same-directory temporary file and atomic no-clobber
hard link. Concurrent collisions are rejected. Marker points and reference points
always refer to the same frozen frame, never later live detections.

## Experimental recordings

`manifest.json` recording_schema_version **1** tracks complete, status, errors,
accepted/written/raw-saved counts and the exact queue-overflow frame ID.
`frames.jsonl` indexes each submitted frame's PNG, timestamps, checksum and status.
`landmarks.jsonl` has corresponding frame_id and recording_config_id, referring to
`configurations.jsonl` snapshots. PNGs are raw, unmirrored, lossless, with no overlays.
Bounded asynchronous queue: 128 data-only entries; at most 16 with images. The
audio callback never reads files. Disk/encoding/overflow failures are explicit;
readers must check complete, which remains false after abnormal termination.

`tracking_previous_state` permits replay to start mid-held without inventing a
fresh pose. Only initial/config-transition state is restored; subsequent tracking
is recomputed from raw images. Replay remaps monotonic timestamps relative to its
controlled clock, preserves source frame IDs, and stores recorded_timestamp_monotonic_s.
Board overrides intentionally discard recorded tracker state. Default audio is
mock. Optional ONNX rerun uses the same backend; no camera or browser inference.
