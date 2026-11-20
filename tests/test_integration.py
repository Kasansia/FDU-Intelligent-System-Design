import base64
import csv
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from http.server import ThreadingHTTPServer
import cv2 as cv
import numpy as np
from app import Service, build_parser, handler_for
from audio_engine import AudioEngine, MockAudioBackend
from hand_tracking import ROOT, HandTracker
from instrument_geometry import InstrumentLayout
from instrument_logic import InstrumentLogic
from pipeline import PerceptionPipeline, annotate_instrument
from recording import Recorder, COLUMNS
from synthetic_input import SyntheticHandTracker, board_image
from tests.helpers import packet
from tools.verify_runtime import check_synthetic_coverage


class IntegrationTests(unittest.TestCase):
    def test_full_chain_with_scripted_hands_real_aruco_both_mirrors_and_layouts(self):
        for mirrored in (False, True):
            for flip in (False, True):
                now = [10.]
                layout = InstrumentLayout(flip_u=flip)
                backend = MockAudioBackend()
                audio = AudioEngine(backend, clock=lambda: now[0])
                self.addCleanup(audio.close)
                pipeline = PerceptionPipeline(SyntheticHandTracker(mirrored, layout), layout=layout,
                                              logic=InstrumentLogic(layout, clock=lambda: now[0]), audio=audio)
                events = []
                frame = board_image()
                for i in range(390):  # 13s deterministic trajectory, no sleeps.
                    now[0] = 10+i/30
                    display, p = pipeline.process(frame, 1792666800+i/30, now[0])
                    backend.pump()
                    self.assertEqual(p['instrument']['status'], 'valid')
                    self.assertEqual(p['frame_id'], p['instrument']['source_frame_id'])
                    self.assertEqual(p['timestamp_monotonic_s'], p['instrument']['source_timestamp_monotonic_s'])
                    self.assertEqual(p['timestamp_unix_s'], 1792666800+i/30)
                    self.assertEqual(np.shape(p['hands'][0]['landmarks_instrument_uv']), (21, 2))
                    json.dumps(p, allow_nan=False)
                    events.extend(p['events'])
                check_synthetic_coverage({e['chord'] for e in events}, {e['direction'] for e in events})
                self.assertEqual(events, backend.events)
                self.assertEqual(len({e['event_id'] for e in events}), len(events))
                self.assertEqual(annotate_instrument(display, p, layout).shape, frame.shape)

    def test_runtime_acceptance_rejects_incomplete_chords_or_strums(self):
        with self.assertRaises(RuntimeError):
            check_synthetic_coverage(['C', 'G', 'Am', 'F'], ['up', 'down'])
        with self.assertRaises(RuntimeError):
            check_synthetic_coverage(['C', 'Dm', 'Em', 'F', 'G', 'Am', 'Bdim'], ['down'])

    def test_real_onnx_plus_markers_and_anatomical_labels(self):
        sample = ROOT/'outputs/test_hands.jpg'
        if not sample.exists():
            self.fail('Download the official hand fixture as documented; real ONNX coverage is required.')
        image = cv.imdecode(np.fromfile(sample, np.uint8), cv.IMREAD_COLOR)
        height, width = image.shape[:2]
        dictionary = cv.aruco.getPredefinedDictionary(cv.aruco.DICT_4X4_50)
        for i, (x, y) in enumerate(((20, 20), (width-80, 20), (20, height-80), (width-80, height-80))):
            image[y-8:y+68, x-8:x+68] = 255  # Background corners; preserve hand image scale.
            image[y:y+60, x:x+60] = cv.cvtColor(cv.aruco.generateImageMarker(dictionary, i, 60), cv.COLOR_GRAY2BGR)
        wrists = []
        for mirrored in (False, True):
            pipeline = PerceptionPipeline(hands=HandTracker(mirror=mirrored))
            _, p = pipeline.process(image)
            self.assertEqual(len(p['hands']), 2)
            self.assertEqual(p['instrument']['status'], 'valid')
            self.assertEqual({h['handedness'] for h in p['hands']}, {'Left', 'Right'})
            wrists.append({h['handedness']: h['landmarks_instrument_uv'][0] for h in p['hands']})
            json.dumps(p, allow_nan=False)
        for role in ('Left', 'Right'):
            np.testing.assert_allclose(wrists[0][role], wrists[1][role], atol=.06)

    def test_recording_all_frames_uv_blanks_and_event_once(self):
        with tempfile.TemporaryDirectory() as root:
            recorder = Recorder(root)
            p = packet(1)
            p['events'] = [{'event_id': 1, 'chord': 'C'}]
            self.assertTrue(recorder.submit(p))
            p2 = packet(2, left=None, right=None)
            self.assertTrue(recorder.submit(p2))
            p3 = packet(3)
            p3['hands'][0]['landmarks_instrument_uv'] = None
            p3['events'] = p['events']
            self.assertTrue(recorder.submit(p3))
            recorder.close()
            self.assertIsNone(recorder.error)
            frames = [json.loads(line) for line in (recorder.folder/'landmarks.jsonl').read_text().splitlines()]
            self.assertEqual(len(frames), 3)
            self.assertEqual(frames[1]['hands'], [])
            with (recorder.folder/'landmarks.csv').open(newline='') as file:
                rows = list(csv.DictReader(file))
            self.assertEqual(list(rows[0]), COLUMNS)
            self.assertEqual(len(rows), 84)
            self.assertEqual(rows[42]['instrument_u'], '')
            self.assertEqual(len((recorder.folder/'events.jsonl').read_text().splitlines()), 1)

    def test_linux_camera_import_has_no_windows_dependency(self):
        import camera_sources
        with patch.object(sys, 'platform', 'linux'), patch.dict(sys.modules, {'cv2_enumerate_cameras': None}):
            self.assertIsInstance(camera_sources.cameras(), list)

    def test_browser_has_no_second_inference_or_camera(self):
        source = (ROOT/'web/index.html').read_text(encoding='utf-8') + (ROOT/'web/app.js').read_text(encoding='utf-8')
        for forbidden in ('getUserMedia', 'detectForVideo', '@mediapipe', 'bilinear(', 'toBoard(', 'AudioContext', 'cdn.jsdelivr'):
            self.assertNotIn(forbidden, source)


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.args = build_parser().parse_args(['--demo', '--audio', 'mock'])
        self.service = Service(self.args)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler_for(self.service))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:'+str(self.server.server_port)

    def tearDown(self):
        self.service.stop.set()
        if self.service.worker.is_alive():
            self.service.worker.join(timeout=5)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.service.set_recording(False)

    def get_json(self, path, method='GET', headers=None):
        with urlopen(Request(self.url+path, method=method, headers=headers or {}), timeout=3) as response:
            return json.load(response)

    def test_compatibility_atomic_snapshot_controls_and_static_assets(self):
        self.service.worker.start()
        deadline = time.monotonic()+5
        while self.service.latest()['status'] == 'starting' and time.monotonic() < deadline:
            self.service.stop.wait(.01)
        state = self.get_json('/api/landmarks')
        self.assertEqual(state['status'], 'running')
        self.assertEqual(state['schema_version'], 2)
        self.assertEqual(self.get_json('/api/state')['source'], 'synthetic')
        snapshot = self.get_json('/api/snapshot')
        image = cv.imdecode(np.frombuffer(base64.b64decode(snapshot['image_base64']), np.uint8), cv.IMREAD_COLOR)
        self.assertEqual(image.shape[:2], (snapshot['packet']['frame_height'], snapshot['packet']['frame_width']))
        with urlopen(self.url+'/frame.jpg') as response:
            self.assertTrue(int(response.headers['X-Frame-Id']) > 0)
        for path in ('/', '/app.js', '/style.css'):
            with urlopen(self.url+path) as response:
                self.assertEqual(response.status, 200)
        with self.assertRaises(HTTPError) as rejected:
            self.get_json('/api/shutdown', 'POST', {'Origin': 'https://untrusted.invalid'})
        self.assertEqual(rejected.exception.code, 403)
        with tempfile.TemporaryDirectory() as root, patch('app.ROOT', Path(root)):
            self.assertTrue(self.get_json('/api/record/start', 'POST')['recording'])
            self.service.stop.wait(.12)
            self.assertFalse(self.get_json('/api/record/stop', 'POST')['recording'])
            self.assertTrue(list(Path(root).glob('outputs/*/landmarks.jsonl')))
        self.get_json('/api/shutdown', 'POST')
        self.assertTrue(self.service.stop.is_set())

    def test_camera_failure_clears_instrument_and_events(self):
        self.service.fail_frame('test camera loss')
        state = self.get_json('/api/landmarks')
        self.assertEqual(state['status'], 'error')
        self.assertEqual(state['hands'], [])
        self.assertEqual(state['events'], [])
        self.assertIsNone(state['instrument']['homography_camera_to_instrument'])
        with self.assertRaises(HTTPError) as rejected:
            urlopen(self.url+'/frame.jpg')
        self.assertEqual(rejected.exception.code, 503)

    def test_audio_failure_keeps_video_alive(self):
        with patch('audio_engine.MockAudioBackend.start', side_effect=RuntimeError('No audio device')):
            self.service.worker.start()
            deadline = time.monotonic()+5
            while self.service.latest()['status'] == 'starting' and time.monotonic() < deadline:
                self.service.stop.wait(.01)
            state = self.get_json('/api/landmarks')
            self.assertEqual(state['status'], 'running')
            self.assertEqual(state['audio_status'], 'error')
            with urlopen(self.url+'/frame.jpg') as response:
                self.assertEqual(response.status, 200)
