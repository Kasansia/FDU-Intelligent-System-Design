import base64
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import cv2 as cv
import numpy as np
from aruco_tracking import ArucoBoardTracker
from board_calibration import CalibrationSessions
from board_definition import BoardDefinition
from instrument_geometry import InstrumentLayout, project
from tests.plane_fixtures import render_board, DEFAULT_INVERSE, six_marker_board
from tests import test_integration


def frozen_packet(image, mirrored=False):
    return dict(status='running', frame_id=7, timestamp_unix_s=1234., mirrored=mirrored,
                instrument=ArucoBoardTracker(board=six_marker_board()).process(image, 10.))


class CalibrationTests(unittest.TestCase):
    def test_frozen_frame_save_reload_and_same_coordinate_system(self):
        for mirrored in (True, False):
            with tempfile.TemporaryDirectory() as root:
                sessions = CalibrationSessions(root)
                image = render_board()
                capture = sessions.capture(image, frozen_packet(image, mirrored))
                original = image.copy()
                image[:] = 0  # A later camera frame cannot replace the frozen detector/image pair.
                reference = project([[0, 0], [1, 0], [1, 1], [0, 1]], DEFAULT_INVERSE)
                preview = sessions.preview(capture['session_id'], reference, 'calibrated', InstrumentLayout())
                self.assertEqual(preview['board']['metadata']['source_frame_id'], 7)
                decoded = cv.imdecode(np.frombuffer(base64.b64decode(preview['image_base64']), np.uint8), cv.IMREAD_COLOR)
                self.assertGreater(decoded.mean(), 150)
                board, path = sessions.save(capture['session_id'], preview['preview_token'])
                loaded = BoardDefinition.load(path)
                self.assertEqual(board.to_dict(), loaded.to_dict())
                result = ArucoBoardTracker(board=loaded).process(original, 20.)
                self.assertEqual(result['status'], 'valid')
                np.testing.assert_allclose(project([[250, 340]], result['homography_camera_to_instrument']), [[.25, .5]], atol=.003)
                self.assertEqual(list(Path(root).glob('*.tmp')), [])
                self.assertFalse(sessions.active())

    def test_invalid_reference_filename_session_and_conflict(self):
        with tempfile.TemporaryDirectory() as root:
            now = [0.]
            sessions = CalibrationSessions(root, clock=lambda: now[0])
            image = render_board()
            capture = sessions.capture(image, frozen_packet(image))
            sid = capture['session_id']
            good = [[100, 90], [700, 90], [700, 590], [100, 590]]
            for pts, name, session in ((good, '../escape', sid), (good, 'CON', sid), (good, 'okay', 'stale'),
                                       ([[0, 0], [10, 10], [20, 20], [30, 30]], 'okay', sid),
                                       ([[0, 0], [100, 100], [100, 0], [0, 100]], 'okay', sid),
                                       ([[-1, 0], [700, 0], [700, 500], [0, 500]], 'okay', sid)):
                with self.assertRaises(ValueError):
                    sessions.preview(session, pts, name, InstrumentLayout())
            preview = sessions.preview(sid, good, 'okay', InstrumentLayout())
            (Path(root)/'okay.json').write_text('existing')
            with self.assertRaises(ValueError):
                sessions.save(sid, preview['preview_token'])
            self.assertEqual((Path(root)/'okay.json').read_text(), 'existing')
            now[0] = 601
            self.assertFalse(sessions.active())
            with self.assertRaises(ValueError):
                sessions.preview(sid, good, 'new', InstrumentLayout())

    def test_capture_quality_replacement_and_cancel(self):
        with tempfile.TemporaryDirectory() as root:
            sessions = CalibrationSessions(root)
            image = render_board()
            p = frozen_packet(image)
            first = sessions.capture(image, p)
            second = sessions.capture(image, p)
            with self.assertRaises(ValueError):
                sessions.cancel(first['session_id'])
            sessions.cancel(second['session_id'])
            with self.assertRaises(ValueError):
                sessions.capture(np.zeros_like(image), p)
            p['instrument']['markers'].append(p['instrument']['markers'][0])
            with self.assertRaises(ValueError):
                sessions.capture(image, p)

    def test_capture_suggests_name_available_in_board_and_preset_directories(self):
        with tempfile.TemporaryDirectory() as root:
            boards, presets = Path(root)/'boards', Path(root)/'presets'
            boards.mkdir()
            presets.mkdir()
            sessions = CalibrationSessions(boards)
            image = render_board()
            packet = frozen_packet(image)

            self.assertEqual(sessions.capture(image, packet)['suggested_board_id'], 'custom_board')
            (boards/'custom_board.json').write_text('existing board', encoding='utf-8')
            self.assertEqual(sessions.capture(image, packet)['suggested_board_id'], 'custom_board_2')
            (presets/'custom_board_2.json').write_text('existing preset', encoding='utf-8')
            self.assertEqual(sessions.capture(image, packet)['suggested_board_id'], 'custom_board_3')


class CalibrationHTTPTests(unittest.TestCase):
    setUp = test_integration.HTTPTests.setUp
    tearDown = test_integration.HTTPTests.tearDown

    def post(self, action, body=None, headers=None):
        data = json.dumps(body or {}).encode()
        with urlopen(Request(self.url+'/api/calibration/'+action, data=data,
                             headers=headers or {'Content-Type': 'application/json'}), timeout=3) as response:
            return json.load(response)

    def test_controls_security_live_capture_and_activation(self):
        with tempfile.TemporaryDirectory() as root:
            self.service.calibration = CalibrationSessions(Path(root)/'config/boards')
            self.service.worker.start()
            deadline = time.monotonic()+5
            while self.service.latest()['status'] == 'starting' and time.monotonic() < deadline:
                self.service.stop.wait(.01)
            for headers in ({'Origin': 'https://evil.invalid'}, {'Sec-Fetch-Site': 'cross-site'}):
                with self.assertRaises(HTTPError) as rejected:
                    self.post('capture', headers=headers)
                self.assertEqual(rejected.exception.code, 403)
            capture = self.post('capture')
            self.service.stop.wait(.1)
            self.assertEqual(self.service.latest()['interaction']['reason'], 'calibration_in_progress')
            preview = self.post('preview', dict(session_id=capture['session_id'], board_id='http_board',
                                               points_raw=[[89.5, 89.5], [549.5, 89.5], [549.5, 389.5], [89.5, 389.5]]))
            with patch('app.ROOT', Path(root)):
                saved = self.post('save', dict(session_id=capture['session_id'], preview_token=preview['preview_token']))
            self.assertTrue(saved['active'])
            self.assertEqual(self.service.pipeline.board.board.board_id, 'http_board')
            self.assertFalse(self.service.calibrating)
            self.assertTrue((Path(root)/'config/boards/http_board.json').exists())
