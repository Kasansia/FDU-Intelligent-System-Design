"""Verify the real capture loop, HTTP contract and recordings without hardware."""
import argparse
import csv
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from http.server import ThreadingHTTPServer
import urllib.request
import cv2 as cv
import numpy as np

from app import Service, handler_for
from hand_tracking import EDGES
from test_aruco import board, hand_packet


class ServiceIntegrationTests(unittest.TestCase):
    def test_same_raw_frame_fusion_recording_and_http(self):
        args = argparse.Namespace(camera=0, backend='dshow', palm_threshold=.6,
                                  hand_threshold=.8, no_mirror=False, width=640, height=480)
        devices = [{'index': 0, 'name': 'Synthetic test camera', 'backend': cv.CAP_DSHOW}]
        image = board()
        with tempfile.TemporaryDirectory() as folder, patch('app.cameras', return_value=devices), patch('app.ROOT', Path(folder)):
            service = Service(args)
            cap = Mock()
            cap.isOpened.return_value = True
            cap.getBackendName.return_value = 'TEST'
            calls = 0

            def read():
                nonlocal calls
                calls += 1
                if calls == 2:
                    # Complete exactly one loop iteration, after the initial open probe.
                    service.stop.set()
                return True, image.copy()

            def infer(raw, timestamp):
                np.testing.assert_array_equal(raw, image)  # Neither tracker gets a flipped input.
                packet = hand_packet()
                packet.update(timestamp_unix_s=timestamp, connections=EDGES)
                return cv.flip(raw, 1), packet

            cap.read.side_effect = read
            hand_tracker = Mock()
            hand_tracker.process.side_effect = infer
            service.set_recording(True)
            with patch('app.cv.VideoCapture', return_value=cap), patch('app.HandTracker', return_value=hand_tracker):
                service.run()
            cap.release.assert_called_once()
            packet = service.latest()
            self.assertEqual(packet['status'], 'running')
            self.assertEqual(packet['schema_version'], 2)
            self.assertEqual(packet['instrument']['status'], 'valid')
            self.assertEqual(packet['inference_ms'], 12.5)
            self.assertGreaterEqual(packet['aruco_ms'], 0)
            self.assertGreaterEqual(packet['vision_total_ms'], packet['aruco_ms'])
            np.testing.assert_allclose(packet['hands'][0]['landmarks_instrument_uv'], [[.5, .5]]*21, atol=.002)
            self.assertFalse(packet['recording'])
            recording = Path(packet['last_recording'])
            recorded = [json.loads(line) for line in (recording / 'landmarks.jsonl').read_text().splitlines()]
            self.assertEqual(len(recorded), 1)
            self.assertEqual(recorded[0]['instrument']['status'], 'valid')
            with (recording / 'landmarks.csv').open(newline='') as source:
                rows = list(csv.DictReader(source))
            self.assertEqual(len(rows), 21)
            self.assertAlmostEqual(float(rows[8]['instrument_u']), .5, places=3)
            server = ThreadingHTTPServer(('127.0.0.1', 0), handler_for(service))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f'http://127.0.0.1:{server.server_port}'
                with urllib.request.urlopen(base + '/api/landmarks', timeout=2) as response:
                    self.assertEqual(json.load(response)['instrument']['status'], 'valid')
                with urllib.request.urlopen(base + '/frame.jpg', timeout=2) as response:
                    jpeg = cv.imdecode(np.frombuffer(response.read(), np.uint8), cv.IMREAD_COLOR)
                    self.assertEqual(jpeg.shape, image.shape)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
