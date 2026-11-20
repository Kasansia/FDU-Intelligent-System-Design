import unittest
import numpy as np
from instrument_preset import InstrumentPreset
from instrument_geometry import InstrumentLayout, overlay_polygons, project, display_to_raw_xy
from instrument_logic import InstrumentLogic, InteractionConfig
from tests.helpers import packet
from tests.plane_fixtures import six_marker_board


class PresetTests(unittest.TestCase):
    def test_concave_regions_outside_unit_square_and_shared_overlay(self):
        preset = InstrumentPreset('custom', {'C': [[-.5, .2], [.2, .2], [.2, .4], [-.2, .4], [-.2, .8], [-.5, .8]]},
                                  [[.6, 0], [1.3, 0], [1.3, 1], [.6, 1]], [[.9, .9], [.9, .1]])
        layout = InstrumentLayout(preset=preset)
        self.assertEqual(layout.chord_at((-.3, .6)), 'C')
        self.assertIsNone(layout.chord_at((.1, .6)))
        self.assertTrue(layout.in_strum_band((1.2, .5)))
        p = packet(0)
        p['instrument']['homography_instrument_to_camera'] = [[600, 0, 100], [0, 500, 80], [.1, -.1, 1]]
        shapes = overlay_polygons(p, layout)
        H = np.linalg.inv(p['instrument']['homography_instrument_to_camera'])
        for name, points in layout.regions()['chords'].items():
            np.testing.assert_allclose(project(shapes[name], H), points, atol=1e-9)

    def test_directed_vertical_line_crossing_uses_normal_motion(self):
        preset = InstrumentPreset('vertical', {'C': [[0, .2], [.4, .2], [.4, .8], [0, .8]]},
                                  [[.6, .1], [1., .1], [1., .9], [.6, .9]], [[.8, .9], [.8, .1]])
        for flipped in (False, True):
            layout = InstrumentLayout(preset=preset, flip_u=flipped)
            now = [0.]
            logic = InstrumentLogic(layout, clock=lambda: now[0])
            def step(t, u):
                now[0] = t
                return logic.process(packet(t, left=layout.local((.2, .5)), right=layout.local((u, .5))))
            step(0, .65)
            step(.11, .65)
            event = step(.2, .95)[1][0]
            self.assertEqual(event['direction'], 'down')
            self.assertEqual(event['chord'], 'C')
            self.assertEqual(step(.4, .65)[1][0]['direction'], 'up')

    def test_board_is_independent_and_bad_preset_rejected(self):
        board = six_marker_board()
        before = board.to_dict()
        InstrumentLayout(preset=InstrumentPreset.default(), flip_u=True)
        self.assertEqual(board.to_dict(), before)
        data = InstrumentPreset.default().to_dict()
        for change in ({'axis': [[0, 0], [0, 0]]}, {'chords': {'D': [[0, 0], [1, 0], [0, 1]]}},
                       {'strum': [[0, 0], [1, 1], [1, 0], [0, 1]]}, {'interaction': {'cooldown_s': -1}},
                       {'chord_frame': [[0, 0], [1, 0], [0, 1]]},
                       {'strum_frame': [[0, 0], [1, 1], [1, 0], [0, 1]]}):
            with self.assertRaises(ValueError):
                InstrumentPreset.from_dict(dict(data, **change))

    def test_strum_horizon_is_rejected_for_new_presets_and_safe_for_legacy(self):
        frame = [[0, 0], [2, 0], [1.5, 1], [.5, 1]]
        axis = [[.2, .5], [1.8, .5]]
        data = InstrumentPreset.default().to_dict()
        for bottom in (2, 2.2):
            with self.subTest(bottom=bottom):
                strum = [[-.1, -.1], [2.1, -.1], [2.1, bottom], [-.1, bottom]]
                with self.assertRaisesRegex(ValueError, 'local projective horizon'):
                    InstrumentPreset.from_dict(dict(data, strum=strum, axis=axis, strum_frame=frame))

        chords = {'C': [[.04, .2], [.18, .2], [.18, .8], [.04, .8]]}
        legacy = InstrumentPreset('legacy', chords, strum, axis, strum_frame=frame)
        layout = InstrumentLayout(preset=legacy)
        for path_enabled in (False, True):
            with self.subTest(path_enabled=path_enabled):
                now = [0.]
                logic = InstrumentLogic(layout, InteractionConfig(strum_path_enabled=path_enabled), clock=lambda: now[0])
                for t, y in ((0, .35), (.11, .35), (.21, .65)):
                    now[0] = t
                    state, events = logic.process(packet(t, right=(1, y)))
                if path_enabled:
                    self.assertEqual(events, [])
                    self.assertEqual(state['strum_reason'], 'outside_strum_zone')
                else:
                    self.assertEqual([event['direction'] for event in events], ['down'])
