"""Two user-selected regions, shared moving plane, persistence and normalized gestures."""
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import numpy as np
from app import Service, build_parser
from aruco_tracking import ArucoBoardTracker
from board_calibration import CalibrationSessions, calibrated_preset_path, region_preset
from board_definition import BoardDefinition
from instrument_geometry import InstrumentLayout, overlay_polygons, project
from instrument_logic import InstrumentLogic
from instrument_preset import InstrumentPreset, frame_transform
from pipeline import PerceptionPipeline
from recording import Recorder
from replay import json_lines, replay_recording
from synthetic_input import SyntheticHandTracker, board_image
from tests.helpers import packet
from tests.plane_fixtures import DEFAULT_INVERSE, render_board
from tests.test_calibration import frozen_packet
from tests import test_calibration


REGIONS = {'chord': [[200, 200], [440, 200], [440, 500], [200, 500]],
           'strum': [[510, 250], [650, 250], [650, 450], [510, 450]]}


class RegionCalibrationTests(unittest.TestCase):
    def preview(self, root, mirrored=False, name='regions'):
        sessions = CalibrationSessions(Path(root)/'boards')
        raw = render_board()
        capture = sessions.capture(raw, frozen_packet(raw, mirrored))
        regions = {k: np.asarray(v)[[1, 0, 3, 2]].tolist() if mirrored else v for k, v in REGIONS.items()}
        preview = sessions.preview(capture['session_id'], None, name, InstrumentLayout(flip_u=True), regions)
        return sessions, capture, preview, regions

    def test_regions_follow_motion_perspective_and_occlusion_in_both_mirrors(self):
        for mirrored in (False, True):
            with self.subTest(mirrored=mirrored), tempfile.TemporaryDirectory() as root:
                sessions, capture, preview, regions = self.preview(root, mirrored)
                board, path = sessions.save(capture['session_id'], preview['preview_token'])
                layout = InstrumentLayout(preset=InstrumentPreset.load(calibrated_preset_path(board, path)))
                self.assertEqual(layout.preset.to_dict(), preview['preset'])
                board = BoardDefinition.load(path)
                for inverse in (DEFAULT_INVERSE, np.array([[530., -60, 150], [50, 410, 80], [.08, -.05, 1.]])):
                    tracker = ArucoBoardTracker(board=board)
                    result = tracker.process(render_board(inverse=inverse, visible=[4, 5, 7, 8]), 10.)
                    self.assertEqual(result['status'], 'valid', result['reason'])
                    p = dict(instrument=result, frame_width=960, mirrored=mirrored)
                    shapes = overlay_polygons(p, layout)
                    for name, original in [('1', [regions['chord'][0], regions['chord'][3]]), ('strum', regions['strum'])]:
                        expected = project(project(original, np.linalg.inv(DEFAULT_INVERSE)), inverse)
                        if mirrored:
                            expected[:, 0] = 959-expected[:, 0]
                        actual = np.asarray(shapes[name])[[0, 3]] if name == '1' else shapes[name]
                        np.testing.assert_allclose(actual, expected, atol=2.)
                    self.assertEqual(tracker.process(render_board(inverse=inverse, visible=[]), 10.1)['status'], 'held')
                    self.assertEqual(tracker.process(render_board(inverse=inverse, visible=[]), 10.4)['status'], 'lost')
                    self.assertEqual(tracker.process(render_board(inverse=inverse), 10.5)['status'], 'valid')

    def test_bad_regions_cancel_stale_preview_and_save_conflicts(self):
        with tempfile.TemporaryDirectory() as root:
            sessions, capture, good, _ = self.preview(root)
            sid = capture['session_id']
            for bad in ({'chord': REGIONS['chord']}, dict(REGIONS, strum=REGIONS['chord']),
                        dict(REGIONS, chord=[[0, 0], [2, 0], [2, 2], [0, 2]]),
                        dict(REGIONS, strum=[[500, 250], [650, 450], [650, 250], [500, 450]]),
                        dict(REGIONS, strum=[[500, 250], [1000, 250], [1000, 450], [500, 450]])):
                with self.assertRaises(ValueError):
                    sessions.preview(sid, None, 'regions', InstrumentLayout(), bad)
                with self.assertRaises(ValueError):
                    sessions.save(sid, good['preview_token'])
            good = sessions.preview(sid, None, 'regions', InstrumentLayout(), REGIONS)
            board_path = Path(root)/'boards/regions.json'
            board_path.parent.mkdir(exist_ok=True)
            board_path.write_text('existing')
            with self.assertRaises(ValueError):
                sessions.save(sid, good['preview_token'])
            self.assertEqual(board_path.read_text(), 'existing')
            self.assertFalse((Path(root)/'presets/regions.json').exists())
            self.assertFalse(list(Path(root).rglob('*.tmp')))
            sessions.cancel(sid)
            self.assertFalse(sessions.active())

    def test_preset_conflict_never_overwrites_and_failed_board_write_rolls_back(self):
        with tempfile.TemporaryDirectory() as root:
            sessions, capture, preview, _ = self.preview(root)
            preset_path = Path(root)/'presets/regions.json'
            preset_path.parent.mkdir()
            preset_path.write_text('existing')
            with self.assertRaises(ValueError):
                sessions.save(capture['session_id'], preview['preview_token'])
            self.assertEqual(preset_path.read_text(), 'existing')
            self.assertFalse((Path(root)/'boards/regions.json').exists())
        with tempfile.TemporaryDirectory() as root:
            sessions, capture, preview, _ = self.preview(root)
            publish = sessions._publish
            def fail_board(target, value):
                if target.parent.name == 'boards':
                    raise OSError('disk failure')
                publish(target, value)
            with patch.object(sessions, '_publish', side_effect=fail_board), self.assertRaises(OSError):
                sessions.save(capture['session_id'], preview['preview_token'])
            self.assertFalse(list(Path(root).rglob('*.json')))

    def test_restart_loads_companion_and_explicit_preset_overrides_it(self):
        with tempfile.TemporaryDirectory() as root:
            sessions, capture, preview, _ = self.preview(root)
            _, path = sessions.save(capture['session_id'], preview['preview_token'])
            for explicit in (False, True):
                args = ['--demo', '--audio', 'mock', '--duration', '.04', '--board', str(path)]
                if explicit:
                    args += ['--preset', str(Path(__file__).resolve().parents[1]/'config/presets/guitar.json')]
                service = Service(build_parser().parse_args(args))
                service.run()
                self.assertEqual(service.pipeline.layout.preset.preset_id, 'guitar' if explicit else 'regions')
                self.assertEqual(service.latest()['status'], 'stopped')

    def test_gestures_and_margins_are_independent_of_region_scale_rotation_and_flip(self):
        velocities = []
        for scale in (.03, .3, 2.):
            transform = np.array([[scale, -.2*scale, .1], [.15*scale, scale, -.2], [.03, .02, 1.]])
            chord = project([[0, 0], [1, 0], [1, 1], [0, 1]], transform)
            strum = project([[2, 0], [3, 0], [3, 1], [2, 1]], transform)
            for flip in (False, True):
                layout = InstrumentLayout(flip_u=flip, preset=region_preset('scaled', chord, strum))
                chord_inverse = np.linalg.inv(frame_transform(chord))
                strum_inverse = np.linalg.inv(frame_transform(strum))
                at = lambda uv, matrix: layout.local(project([uv], matrix)[0])
                self.assertIsNone(layout.chord_at(at([.005, .5], chord_inverse), margin=.015))
                now = [0.]
                logic = InstrumentLogic(layout, clock=lambda: now[0])
                for i, name in enumerate(('C', 'Dm', 'Em', 'F', 'G', 'Am', 'Bdim')):
                    events = []
                    for t, v in ((0., .3), (.11, .3), (.2, .7), (.4, .3)):
                        now[0] = i+t
                        _, emitted = logic.process(packet(now[0], left=at([(i+.5)/7, .5], chord_inverse), right=at([.5, v], strum_inverse)))
                        events.extend(emitted)
                    self.assertEqual([e['direction'] for e in events], ['down', 'up'])
                    self.assertEqual({e['chord'] for e in events}, {name})
                    velocities.append([e['velocity'] for e in events])
        # Integer MIDI rounding can differ by one at a half-step after float32 fitting.
        self.assertTrue(all(np.max(np.abs(np.asarray(value)-velocities[0])) <= 1 for value in velocities))

    def test_custom_region_recording_replays_normalized_events(self):
        with tempfile.TemporaryDirectory() as root:
            preset = region_preset('recorded', [[.04, .2], [.6, .2], [.6, .8], [.04, .8]],
                                   [[.68, .15], [.9, .15], [.9, .85], [.68, .85]])
            layout = InstrumentLayout(preset=preset)
            now = [1.]
            pipeline = PerceptionPipeline(hands=SyntheticHandTracker(), layout=layout, logic=InstrumentLogic(layout, clock=lambda: now[0]))
            recorder = Recorder(root, raw_frames=True)
            expected = []
            try:
                for i in range(18):
                    now[0] = 1+i*.08
                    raw = board_image()
                    _, p = pipeline.process(raw, 1000+i*.08, now[0])
                    self.assertTrue(recorder.submit(p, raw, pipeline.configuration()))
                    expected.extend((e['chord'], e['direction'], e['velocity']) for e in p['events'])
            finally:
                recorder.close()
            target = Path(root)/'replay.jsonl'
            replay_recording(recorder.folder, target)
            actual = [(e['chord'], e['direction'], e['velocity']) for p in json_lines(target) for e in p['events']]
            self.assertTrue(expected)
            self.assertEqual(actual, expected)


class RegionHTTPTests(unittest.TestCase):
    setUp = test_calibration.CalibrationHTTPTests.setUp
    tearDown = test_calibration.CalibrationHTTPTests.tearDown
    post = test_calibration.CalibrationHTTPTests.post

    def test_two_region_save_activates_layout_logic_and_recorded_configuration(self):
        with tempfile.TemporaryDirectory() as root:
            self.service.calibration = CalibrationSessions(Path(root)/'config/boards')
            self.service.worker.start()
            deadline = time.monotonic()+5
            while self.service.latest()['status'] == 'starting' and time.monotonic() < deadline:
                self.service.stop.wait(.01)
            capture = self.post('capture')
            regions = {'chord': [[330, 130], [150, 130], [150, 330], [330, 330]],
                       'strum': [[490, 160], [400, 160], [400, 310], [490, 310]]}
            preview = self.post('preview', dict(session_id=capture['session_id'], board_id='selected', regions_raw=regions))
            with self.service.pipeline_lock:
                self.service.pipeline.logic.event_id = 42
            with patch('app.ROOT', Path(root)):
                saved = self.post('save', dict(session_id=capture['session_id'], preview_token=preview['preview_token']))
            self.assertEqual(saved['preset_saved'], 'config\\presets\\selected.json' if '\\' in saved['saved'] else 'config/presets/selected.json')
            self.assertIs(self.service.pipeline.layout, self.service.pipeline.logic.layout)
            self.assertFalse(self.service.pipeline.layout.flip_u)
            self.assertEqual(self.service.pipeline.logic.event_id, 42)
            self.assertEqual(self.service.pipeline.configuration()['preset'], preview['preset'])
            self.assertFalse(self.service.calibrating)
            self.service.stop.wait(.12)
            self.assertEqual(self.service.latest()['layout']['preset_id'], 'selected')
