"""Repeatable timed software run, never a substitute for live-board acceptance.

synthetic: scripted hands and real ArUco. onnx: official static hand photograph
with generated corner markers and real OpenCV Zoo inference. Both use production
geometry, logic, audio, overlay rendering and asynchronous recording.
"""
import argparse
import json
from pathlib import Path
import platform
import sys
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cv2 as cv
import numpy as np
from audio_engine import AudioEngine, MockAudioBackend, SoundDeviceBackend
from hand_tracking import ROOT, HandTracker
from pipeline import PerceptionPipeline, annotate_instrument
from recording import Recorder
from synthetic_input import SyntheticHandTracker, board_image
from timing import monotonic_s


def check_synthetic_coverage(chords, directions):
    expected_chords = {'C', 'Dm', 'Em', 'F', 'G', 'Am', 'Bdim'}
    if set(chords) != expected_chords or set(directions) != {'up', 'down'}:
        raise RuntimeError(f'Expected all seven C-major chords and both strum directions; '
                           f'observed chords={sorted(chords)}, directions={sorted(directions)}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=['synthetic', 'onnx'], default='synthetic')
    parser.add_argument('--seconds', type=float, default=32)
    parser.add_argument('--audio', choices=['mock', 'sounddevice'], default='mock')
    parser.add_argument('--device')
    parser.add_argument('--sample', help='Optional schema-v2 example JSON file')
    args = parser.parse_args()
    if args.seconds <= 0:
        parser.error('--seconds must be positive')
    cv.setNumThreads(4)
    frame = board_image()
    tracker = SyntheticHandTracker()
    if args.mode == 'onnx':
        sample = ROOT/'outputs/test_hands.jpg'
        if not sample.exists():
            raise RuntimeError('Download the README official hand test fixture first')
        frame = cv.imdecode(np.fromfile(sample, np.uint8), cv.IMREAD_COLOR)
        h, w = frame.shape[:2]
        dictionary = cv.aruco.getPredefinedDictionary(cv.aruco.DICT_4X4_50)
        for i, (x, y) in enumerate(((20, 20), (w-80, 20), (20, h-80), (w-80, h-80))):
            frame[y-8:y+68, x-8:x+68] = 255
            frame[y:y+60, x:x+60] = cv.cvtColor(cv.aruco.generateImageMarker(dictionary, i, 60), cv.COLOR_GRAY2BGR)
        tracker = HandTracker()
    device = int(args.device) if args.device and args.device.isdigit() else args.device
    backend = MockAudioBackend() if args.audio == 'mock' else SoundDeviceBackend(device)
    audio = AudioEngine(backend)
    recorder = Recorder(ROOT/'outputs')
    pipeline = PerceptionPipeline(hands=tracker, audio=audio)
    events, times, inference, aruco = [], [], [], []
    frames = valid = two_hands = 0
    started = time.monotonic()
    try:
        while time.monotonic()-started < args.seconds:
            tick = monotonic_s()
            tick_perf = time.perf_counter()
            display, packet = pipeline.process(frame, time.time(), tick)
            packet['source'] = 'synthetic' if args.mode == 'synthetic' else 'static_photo_with_synthetic_markers'
            if args.audio == 'mock':
                backend.pump()
            packet['audio'] = audio.snapshot()
            packet['audio_status'] = packet['audio']['status']
            encoded, _ = cv.imencode('.jpg', annotate_instrument(display, packet, pipeline.layout))
            if not encoded:
                raise RuntimeError('JPEG encode failed')
            recorder.submit(packet)
            json.dumps(packet, allow_nan=False)
            frames += 1
            valid += packet['instrument']['status'] == 'valid'
            two_hands += len(packet['hands']) == 2
            events.extend(packet['events'])
            times.append((time.perf_counter()-tick_perf)*1000)
            inference.append(packet['inference_ms'])
            aruco.append(packet['instrument']['aruco_ms'])
            if args.mode == 'synthetic':
                time.sleep(max(0, 1/30-(time.perf_counter()-tick_perf)))
        elapsed = time.monotonic()-started
        if args.sample:
            target = Path(args.sample)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(packet, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')
        recorder.close()
        report = {'mode': args.mode, 'hardware_acceptance': 'NOT TESTED',
                  'platform': platform.platform(), 'python': platform.python_version(), 'opencv': cv.__version__,
                  'elapsed_s': elapsed, 'frames': frames, 'fps': frames/elapsed,
                  'valid_board_frames': valid, 'two_hand_frames': two_hands,
                  'generated_events': len(events), 'unique_event_ids': len({e['event_id'] for e in events}),
                  'chords': sorted({e['chord'] for e in events}), 'directions': sorted({e['direction'] for e in events}),
                  'processing_ms_p50': float(np.median(times)), 'processing_ms_p95': float(np.percentile(times, 95)),
                  'inference_ms_p50': float(np.median(inference)), 'aruco_ms_p50': float(np.median(aruco)),
                  'audio': audio.snapshot(), 'recording_error': recorder.error, 'recorded_frames': recorder.frames,
                  'recording_folder': str(recorder.folder)}
        report_path = recorder.folder/'runtime_report.json'
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if (valid != frames or two_hands != frames or recorder.error or recorder.frames != frames or
                report['audio']['status'] == 'error' or report['audio']['dropped_events'] or
                report['audio']['played_events'] != len(events)):
            raise RuntimeError('Runtime acceptance failed; see report')
        if args.mode == 'onnx' and events:
            raise RuntimeError('Static photograph caused a false strum')
        if args.mode == 'synthetic' and args.seconds >= 12:
            check_synthetic_coverage(report['chords'], report['directions'])
    finally:
        recorder.close()
        audio.close()


if __name__ == '__main__':
    main()
