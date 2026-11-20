"""Deterministic lossless experiment replay with an explicit relative clock."""
from collections import Counter
from copy import deepcopy
import hashlib
import itertools
import json
from pathlib import Path
import time
import cv2 as cv
import numpy as np
from aruco_tracking import ArucoBoardTracker, TrackingConfig
from audio_engine import AudioEngine, MockAudioBackend, SoundDeviceBackend
from board_definition import BoardDefinition
from hand_tracking import HandTracker
from instrument_geometry import InstrumentLayout, fuse_hands
from instrument_logic import InstrumentLogic, InteractionConfig
from instrument_preset import InstrumentPreset
from music import Performance


def json_lines(path):
    with Path(path).open(encoding='utf-8') as stream:
        for line in stream:
            yield json.loads(line)


def replay_recording(directory, output, board_override=None, preset_override=None, rerun_hands=False,
                     audio_mode='mock', allow_incomplete=False):
    directory, output = Path(directory), Path(output)
    if output.resolve() in {(directory/name).resolve() for name in ('landmarks.jsonl', 'frames.jsonl', 'configurations.jsonl', 'manifest.json', 'events.jsonl')}:
        raise ValueError('Replay output must not overwrite a source recording')
    manifest = json.loads((directory/'manifest.json').read_text(encoding='utf-8'))
    if not manifest.get('raw_frames'):
        raise ValueError('Recording has no raw frames; enable experimental capture first')
    if not manifest.get('complete') and not allow_incomplete:
        raise ValueError('Incomplete recording; inspect manifest or explicitly use --allow-incomplete')
    if audio_mode not in ('mock', 'sounddevice'):
        raise ValueError('Replay audio must be mock or explicitly sounddevice')
    configs = {v['config_id']: v['configuration'] for v in json_lines(directory/'configurations.jsonl')}
    clock = [1.]
    backend = MockAudioBackend() if audio_mode == 'mock' else SoundDeviceBackend()
    audio = AudioEngine(backend, clock=lambda: clock[0])
    tracker = logic = hand_tracker = None
    active = None
    spatial_config = None
    first_time = previous_time = previous_id = None
    wall_start = time.monotonic()
    states, frames, missing, events = Counter(), 0, [], []
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with output.open('x', encoding='utf-8') as stream:
            for original, index in itertools.zip_longest(json_lines(directory/'landmarks.jsonl'), json_lines(directory/'frames.jsonl')):
                if original is None or index is None or original['frame_id'] != index['frame_id']:
                    raise ValueError('Frame index and packet sequence are not aligned')
                if original['timestamp_monotonic_s'] != index['timestamp_monotonic_s'] or original['timestamp_unix_s'] != index['timestamp_unix_s']:
                    raise ValueError('Frame timestamps do not match the packet')
                source_time = float(original['timestamp_monotonic_s'])
                if not np.isfinite(source_time) or (previous_time is not None and source_time <= previous_time) or (
                        previous_id is not None and original['frame_id'] <= previous_id):
                    raise ValueError('Recording timestamps/frame IDs must increase')
                previous_time, previous_id = source_time, original['frame_id']
                first_time = source_time if first_time is None else first_time
                if index['status'] != 'saved':
                    missing.append(original['frame_id'])
                    if allow_incomplete:
                        continue
                    raise ValueError('Raw frame failed during recording')
                target = (directory/index['path']).resolve()
                if not target.is_relative_to((directory/'frames').resolve()):
                    raise ValueError('Raw frame path escapes recording frames directory')
                encoded = target.read_bytes()
                if hashlib.sha256(encoded).hexdigest() != index.get('sha256'):
                    raise ValueError('Raw frame checksum mismatch')
                raw = cv.imdecode(np.frombuffer(encoded, np.uint8), cv.IMREAD_COLOR)
                if raw is None or raw.shape[:2] != (original['frame_height'], original['frame_width']):
                    raise ValueError('Raw frame dimensions/decode mismatch')
                config_id = original.get('recording_config_id')
                if config_id not in configs:
                    raise ValueError('Missing recorded board/preset configuration')
                if active != config_id:
                    config = configs[config_id]
                    spatial = {k: v for k, v in config.items() if k != 'performance'}
                    performance = Performance(**config.get('performance', {}))
                    if spatial != spatial_config:
                        board = board_override or (BoardDefinition.from_dict(config['board']) if config.get('board') else None)
                        # Historical schema-1 recordings intentionally retain their four cells.
                        preset = preset_override or InstrumentPreset.from_dict(config['preset'])
                        layout = InstrumentLayout(preset=preset, flip_u=config.get('flip_u', False))
                        tracker = ArucoBoardTracker(board=board, learn_legacy=True, config=TrackingConfig(**config['tracking']))
                        if board_override is None and original.get('tracking_previous_state'):
                            tracker.restore_state(original['tracking_previous_state'], 1.-first_time)
                        last_event_id = logic.event_id if logic is not None else 0
                        interaction = dict(config['interaction'] if preset_override is None else preset.interaction)
                        if preset_override is None:
                            interaction.setdefault('strum_path_enabled', False)
                            interaction.setdefault('selection_margin_fraction', None)
                        logic = InstrumentLogic(layout, InteractionConfig(**interaction), clock=lambda: clock[0], performance=performance)
                        logic.event_id = last_event_id
                        spatial_config = spatial
                    else:
                        logic.set_performance(performance)
                    audio.set_performance_revision(performance.revision)
                    active = config_id
                clock[0] = 1.+source_time-first_time
                if audio_mode == 'sounddevice':
                    time.sleep(max(0, source_time-first_time-(time.monotonic()-wall_start)))
                started = time.perf_counter()
                if rerun_hands:
                    if hand_tracker is None or hand_tracker.mirror != original['mirrored']:
                        hand_tracker = HandTracker(mirror=original['mirrored'])
                    _, packet = hand_tracker.process(raw, original['timestamp_unix_s'])
                    packet['frame_id'] = original['frame_id']
                else:
                    packet = deepcopy(original)
                instrument = tracker.process(raw, clock[0])
                instrument['source_frame_id'] = original['frame_id']
                fuse_hands(packet, instrument)
                packet.update(timestamp_monotonic_s=clock[0], status='running', source='replay',
                              recorded_timestamp_monotonic_s=source_time, layout=layout.regions(),
                              performance=logic.performance.snapshot(),
                              replay_hands='rerun_onnx' if rerun_hands else 'recorded_landmarks',
                              vision_total_ms=round((time.perf_counter()-started)*1000, 3))
                recorded_reason = original.get('interaction', {}).get('reason')
                if recorded_reason in ('calibration_in_progress', 'layout_requires_calibration'):
                    packet['interaction'], packet['events'] = logic.invalidate(recorded_reason), []
                else:
                    packet['interaction'], packet['events'] = logic.process(packet)
                events.extend(packet['events'])
                for event in packet['events']:
                    audio.submit(event)
                if audio_mode == 'mock':
                    backend.pump()
                packet['audio'] = audio.snapshot()
                packet['audio_status'] = packet['audio']['status']
                packet.pop('recent_events', None)
                stream.write(json.dumps(packet, allow_nan=False)+'\n')
                states[instrument['status']] += 1
                frames += 1
        return dict(frames=frames, states=dict(states), generated_events=len(events), missing_frames=missing,
                    source_complete=manifest.get('complete', False), source_dropped_frames=manifest.get('dropped_frames', []),
                    audio=audio.snapshot(), output=str(output))
    finally:
        audio.close()


def analyze_packets(packets):
    states, visibility = Counter(), Counter()
    residuals, latencies, uv_steps, recovery = [], [], [], []
    old_points = {}
    failure_start = None
    count = 0
    for p in packets:
        count += 1
        board = p['instrument']
        status = board['status']
        states[status] += 1
        q = board.get('pose_quality', {})
        visibility[q.get('visible_marker_count', len(board.get('visible_ids', [])))] += 1
        if q.get('reprojection_rmse_px') is not None:
            residuals.append(q['reprojection_rmse_px'])
        if p.get('vision_total_ms') is not None:
            latencies.append(p['vision_total_ms'])
        timestamp = p['timestamp_monotonic_s']
        if status != 'valid' and failure_start is None:
            failure_start = timestamp
        if status == 'valid' and failure_start is not None:
            recovery.append(timestamp-failure_start)
            failure_start = None
        current = {}
        if status == 'valid':
            for hand in p['hands']:
                uv = hand.get('landmarks_instrument_uv')
                if uv is None:
                    continue
                key = (hand['track_id'], hand['handedness'])
                current[key] = np.asarray(uv)
                if key in old_points:
                    uv_steps.extend(np.linalg.norm(current[key]-old_points[key], axis=1).tolist())
        old_points = current
    stats = lambda values: None if not values else dict(mean=float(np.mean(values)), p50=float(np.median(values)), p95=float(np.percentile(values, 95)))
    return dict(frames=count, state_counts=dict(states), state_ratios={k: v/count for k, v in states.items()},
                visible_marker_histogram=dict(visibility), reprojection_rmse_px=stats(residuals),
                vision_total_ms=stats(latencies), uv_step=stats(uv_steps),
                uv_step_note='Frame-to-frame displacement; only interpretable as jitter when hand and board are fixed relative to each other.',
                recovery_durations_s=recovery, unrecovered_at_end=failure_start is not None,
                hardware_acceptance='NOT TESTED')
