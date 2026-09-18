"""Real ONNX regression checks using the official MediaPipe example photograph."""
import json
from pathlib import Path
import sys
import unittest
import cv2 as cv
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hand_tracking import ROOT, HandTracker


class PipelineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cv.setNumThreads(4)
        cls.tracker = HandTracker()
        sample = ROOT / 'outputs/test_hands.jpg'
        if not sample.exists():
            raise RuntimeError('Missing sample: download woman_hands.jpg as described in README.md')
        cls.image = cv.imdecode(np.fromfile(sample, dtype=np.uint8), cv.IMREAD_COLOR)

    def test_two_hands_and_coordinate_contract(self):
        _, packet = self.tracker.process(self.image)
        self.assertEqual(len(packet['hands']), 2)
        self.assertEqual({h['handedness'] for h in packet['hands']}, {'Left', 'Right'})
        for hand in packet['hands']:
            for field in ['landmarks_px', 'landmarks_normalized', 'landmarks_world_m']:
                data = np.array(hand[field])
                self.assertEqual(data.shape, (21, 3))
                self.assertTrue(np.isfinite(data).all())
            np.testing.assert_allclose(np.array(hand['landmarks_normalized']) *
                                       [packet['frame_width'], packet['frame_height'], packet['frame_width']],
                                       hand['landmarks_px'])
        self.assertEqual(len(packet['landmark_names']), 21)
        json.dumps(packet, allow_nan=False)
        _, repeated = self.tracker.process(self.image)
        self.assertEqual([h['track_id'] for h in packet['hands']], [h['track_id'] for h in repeated['hands']])

    def test_hand_loss_clears_nodes(self):
        self.tracker.process(self.image)
        _, empty = self.tracker.process(np.zeros_like(self.image))
        self.assertEqual(empty['hands'], [])

    def test_handedness_without_mirror(self):
        other = HandTracker(mirror=False)
        _, packet = other.process(self.image)
        self.assertFalse(packet['mirrored'])
        self.assertEqual(len(packet['hands']), 2)
        self.assertEqual({h['handedness'] for h in packet['hands']}, {'Left', 'Right'})


if __name__ == '__main__':
    unittest.main()
