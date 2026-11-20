"""Realistic sampled sweeps: fast paths, missed axes, tracking loss and cell interiors."""
import unittest
import numpy as np
from board_calibration import region_preset
from instrument_geometry import InstrumentLayout, project
from instrument_logic import InstrumentLogic, InteractionConfig
from pipeline import annotate_instrument
from tests.helpers import packet


class TrajectoryTests(unittest.TestCase):
    def setup_logic(self, flipped=False, matrix=None, legacy=False):
        self.now = 0.
        self.matrix = np.eye(3) if matrix is None else matrix
        selection = project([[0, 0], [1, 0], [1, 1], [0, 1]], self.matrix)
        strum = project([[2, 0], [3, 0], [3, 1], [2, 1]], self.matrix)
        self.layout = InstrumentLayout(flip_u=flipped, preset=region_preset('gesture', selection, strum))
        self.logic = InstrumentLogic(self.layout, InteractionConfig(strum_path_enabled=not legacy), clock=lambda: self.now)

    def at(self, p):
        return self.layout.local(project([p], self.matrix)[0])

    def step(self, t, x=.5, y=.3, left=True, right=True, board='valid', identity=2):
        self.now = t
        p = packet(t, left=self.at((.07, .5)) if left else None,
                   right=self.at((2+x, y)) if right else None, board=board)
        if right:
            p['hands'][-1]['track_id'] = identity
        return self.logic.process(p)

    def arm(self, y=.3, x=.5):
        self.step(0, x=x, y=y)
        self.assertTrue(self.step(.11, x=x, y=y)[0]['selection_valid'])

    def test_fast_sweeps_with_no_sample_inside_region_both_directions_and_transforms(self):
        matrices = [np.eye(3), np.array([[.2, -.7, .1], [.8, .15, -.4], [.02, .01, 1]])]
        for matrix in matrices:
            for flip in (False, True):
                for start, end in ((.15, .85), (-.1, 1.1)):
                    with self.subTest(flip=flip, start=start):
                        self.setup_logic(flip, matrix)
                        self.arm(start)
                        state, events = self.step(.24, y=end)
                        self.assertEqual(len(events), 1)
                        self.assertEqual(events[0]['direction'], 'down')
                        self.assertEqual(state['strum_reason'], 'strum_triggered')
                        self.assertEqual(self.step(.3, y=end)[1], [])
                        self.assertEqual(self.step(.43, y=start)[1][0]['direction'], 'up')

    def test_legacy_rule_reproduces_missed_fast_sweep(self):
        self.setup_logic(legacy=True)
        self.arm(.15)
        self.assertEqual(self.step(.24, y=.85)[1], [])

    def test_passing_next_to_axis_or_along_same_side_never_triggers(self):
        self.setup_logic()
        self.arm(.35, x=1.15)
        state, events = self.step(.21, x=1.15, y=.65)
        self.assertEqual(events, [])
        self.assertEqual(state['strum_reason'], 'strum_missed_axis')
        self.setup_logic()
        self.arm(.3)
        self.assertEqual(self.step(.21, x=.8, y=.3)[1], [])

    def test_curved_path_does_not_use_imaginary_anchor_to_endpoint_shortcut(self):
        self.setup_logic()
        self.arm(.3, x=-.2)
        self.assertEqual(self.step(.21, x=-.2, y=.52)[1], [])
        self.assertEqual(self.step(.31, x=.2, y=.54)[1], [])
        self.assertEqual(self.step(.41, x=.2, y=.7)[1], [])

    def test_real_axis_crossing_is_remembered_until_hysteresis_threshold(self):
        self.setup_logic()
        self.arm(.3)
        self.assertEqual(self.step(.21, y=.52)[1], [])
        self.assertEqual(self.step(.25, y=.54)[1], [])
        self.assertEqual(len(self.step(.29, y=.65)[1]), 1)
        for i in range(6):
            self.assertEqual(self.step(.32+i*.03, y=.5+(-1)**i*.025)[1], [])

    def test_return_across_axis_clears_pending_crossing_in_both_directions(self):
        for direction in (-1, 1):
            with self.subTest(direction=direction):
                self.setup_logic()
                self.arm(.5-.2*direction, x=.90)
                self.assertEqual(self.step(.16, x=.90, y=.5+.02*direction)[1], [])
                self.assertEqual(self.step(.19, x=.90, y=.5-.02*direction)[1], [])
                self.assertEqual(self.step(.23, x=1.15, y=.5-.02*direction)[1], [])
                state, events = self.step(.27, x=1.15, y=.5+.16*direction)
                self.assertEqual(events, [])
                self.assertEqual(state['strum_reason'], 'strum_missed_axis')

    def test_axis_touch_keeps_the_approach_side_until_the_next_sample(self):
        self.setup_logic()
        self.arm(.30, x=1.15)
        self.assertEqual(self.step(.16, x=1.15, y=.52)[1], [])
        self.assertEqual(self.step(.20, x=.90, y=.52)[1], [])
        self.assertEqual(self.step(.24, x=.90, y=.50)[1], [])
        state, events = self.step(.28, x=.90, y=.65)
        self.assertEqual(events, [])
        self.assertEqual(state['strum_reason'], 'strum_missed_axis')

        self.setup_logic()
        self.arm(.30)
        self.assertEqual(self.step(.21, y=.50)[1], [])
        self.assertEqual(self.step(.29, y=.65)[1][0]['direction'], 'down')

    def test_missing_data_and_outliers_cannot_be_bridged(self):
        for failure in ({'right':False}, {'left':False}, {'board':'held'}, {'board':'lost'}, {'identity':8}):
            self.setup_logic()
            self.arm(.15)
            self.assertEqual(self.step(.16, y=.4, **failure)[1], [])
            self.assertEqual(self.step(.24, y=.85)[1], [])
        self.setup_logic()
        self.arm(.15)
        state, events = self.step(.12, y=.85)
        self.assertEqual(events, [])
        self.assertEqual(state['strum_reason'], 'strum_motion_outlier')

    def test_diagnostics_separate_missing_right_from_unselected_left(self):
        self.setup_logic()
        self.arm()
        self.assertEqual(self.step(.2, right=False)[0]['strum_reason'], 'right_hand_missing')
        self.assertEqual(self.step(.3, left=False)[0]['strum_reason'], 'selection_not_ready')
        self.setup_logic()
        self.arm()
        self.assertEqual(self.step(.2, x=2)[0]['strum_reason'], 'outside_strum_zone')


class SelectionInteriorTests(unittest.TestCase):
    def test_each_cell_selects_across_its_interior_without_touching_boundary(self):
        for flip in (False, True):
            layout = InstrumentLayout(flip_u=flip)
            for degree in range(1, 8):
                for x in (.1, .5, .9):
                    for y in (.1, .5, .9):
                        now = [0.]
                        logic = InstrumentLogic(layout, clock=lambda: now[0])
                        position = layout.local((.04+(degree-1+x)*.08, .2+y*.6))
                        for t in (0, .11):
                            now[0] = t
                            state, events = logic.process(packet(t, left=position, right=None))
                        self.assertTrue(state['selection_valid'], (degree, x, y))
                        self.assertEqual(state['selected_degree'], degree)
                        self.assertEqual(events, [])

    def test_overlay_fills_selected_polygon_instead_of_only_outline(self):
        layout = InstrumentLayout()
        p = packet(0, left=None, right=None)
        p['instrument'].update(markers=[], homography_instrument_to_camera=[[460, 0, 90], [0, 300, 90], [0, 0, 1]])
        p['interaction'] = dict(selected_degree=1, selection_valid=True)
        image = annotate_instrument(np.zeros((480, 640, 3), np.uint8), p, layout)
        y = int(90+.35*300)
        active = image[y, int(90+.08*460)]
        inactive = image[y, int(90+.16*460)]
        self.assertTrue(np.any(active))
        self.assertGreater(int(active[2]), int(inactive[2]))
        self.assertGreater(int(inactive[0]), int(active[0]))
