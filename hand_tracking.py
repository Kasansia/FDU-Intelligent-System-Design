"""OpenCV Zoo inference and a reusable, JSON-compatible landmark contract."""
from pathlib import Path
import sys
import time
import cv2 as cv
import numpy as np

ROOT = Path(__file__).resolve().parent
ZOO = ROOT / 'vendor' / 'opencv_zoo_runtime' / 'models'
sys.path[:0] = [str(ZOO / 'palm_detection_mediapipe'), str(ZOO / 'handpose_estimation_mediapipe')]
from mp_palmdet import MPPalmDet
from mp_handpose import MPHandPose

NAMES = ['wrist'] + [f'{finger}_{joint}' for finger, joints in
    [('thumb', ['cmc', 'mcp', 'ip', 'tip']), ('index', ['mcp', 'pip', 'dip', 'tip']),
     ('middle', ['mcp', 'pip', 'dip', 'tip']), ('ring', ['mcp', 'pip', 'dip', 'tip']),
     ('pinky', ['mcp', 'pip', 'dip', 'tip'])] for joint in joints]
EDGES = [(0, 1), (1, 2), (2, 3), (3, 4), (0, 5), (5, 6), (6, 7), (7, 8),
         (5, 9), (9, 10), (10, 11), (11, 12), (9, 13), (13, 14), (14, 15),
         (15, 16), (13, 17), (0, 17), (17, 18), (18, 19), (19, 20)]


class HandTracker:
    def __init__(self, palm_threshold=0.6, hand_threshold=0.8, max_hands=2, mirror=True):
        self.palm = MPPalmDet(str(ROOT / 'models/palm_detection_mediapipe_2023feb.onnx'),
                              scoreThreshold=palm_threshold, nmsThreshold=0.3)
        self.pose = MPHandPose(str(ROOT / 'models/handpose_estimation_mediapipe_2023feb.onnx'),
                               confThreshold=hand_threshold)
        self.max_hands, self.mirror = max_hands, mirror
        self.tracks, self.next_id, self.frame_id = {}, 1, 0

    def process(self, bgr, timestamp=None):
        """Returns (display-coordinate BGR image, packet). No smoothing or audio."""
        started = time.perf_counter()
        captured = time.time() if timestamp is None else timestamp
        frame = cv.flip(bgr, 1) if self.mirror else bgr.copy()
        height, width = frame.shape[:2]
        hands = []
        palms = self.palm.infer(frame)
        for palm in sorted(palms, key=lambda p: -p[-1]):
            if len(hands) >= self.max_hands:
                break
            # Ignore boxes with no area in the actual image (invalid crop).
            x1, y1, x2, y2 = palm[:4]
            if min(x2, width) <= max(0, x1) or min(y2, height) <= max(0, y1):
                continue
            result = self.pose.infer(frame, palm)
            if result is None or not np.isfinite(result).all():
                continue
            screen = result[4:67].reshape(21, 3)
            world = result[67:130].reshape(21, 3)
            right = float(result[130])
            # MediaPipe handedness assumes a mirrored/selfie input.
            if not self.mirror:
                right = 1.0 - right
            hands.append({
                'handedness': 'Right' if right > 0.5 else 'Left',
                'handedness_score': max(right, 1.0 - right),
                'confidence': float(result[131]), 'palm_confidence': float(palm[-1]),
                'bbox_px': result[:4].tolist(),
                'landmarks_px': screen.tolist(),
                'landmarks_normalized': (screen / [width, height, width]).tolist(),
                'landmarks_world_m': world.tolist(),
            })
        # Short-lived spatial IDs are independent of the left/right prediction.
        now = time.monotonic()
        self.tracks = {k: v for k, v in self.tracks.items() if now - v[1] < 0.5}
        pairs = sorted((float(np.linalg.norm(np.array(h['landmarks_normalized'][0][:2]) - pos)), i, tid)
                       for i, h in enumerate(hands) for tid, (pos, _) in self.tracks.items())
        assigned, used = {}, set()
        for distance, i, tid in pairs:
            if distance < 0.2 and i not in assigned and tid not in used:
                assigned[i] = tid
                used.add(tid)
        for i, hand in enumerate(hands):
            if i not in assigned:
                assigned[i] = self.next_id
                self.next_id += 1
            hand['track_id'] = assigned[i]
            self.tracks[assigned[i]] = (np.array(hand['landmarks_normalized'][0][:2]), now)
        self.frame_id += 1
        packet = {'schema_version': 1, 'frame_id': self.frame_id,
                  'timestamp_unix_s': captured, 'frame_width': width, 'frame_height': height,
                  'mirrored': self.mirror, 'inference_ms': round((time.perf_counter()-started)*1000, 2),
                  'landmark_names': NAMES, 'connections': EDGES, 'hands': hands}
        return frame, packet


def annotate(frame, packet):
    canvas = frame.copy()
    for hand in packet['hands']:
        color = (200, 224, 72) if hand['handedness'] == 'Left' else (90, 172, 255)
        points = np.rint(np.array(hand['landmarks_px'])[:, :2]).astype(int)
        for a, b in EDGES:
            cv.line(canvas, tuple(points[a]), tuple(points[b]), color, 2, cv.LINE_AA)
        for i, point in enumerate(points):
            xy = tuple(point)
            cv.circle(canvas, xy, 4, color, -1, cv.LINE_AA)
            cv.putText(canvas, str(i), (xy[0]+5, xy[1]-5), cv.FONT_HERSHEY_SIMPLEX, .35, (15, 15, 15), 3, cv.LINE_AA)
            cv.putText(canvas, str(i), (xy[0]+5, xy[1]-5), cv.FONT_HERSHEY_SIMPLEX, .35, (255, 255, 255), 1, cv.LINE_AA)
        x, y = points[0]
        label = f"{hand['handedness']} #{hand['track_id']} {hand['confidence']:.0%}"
        cv.putText(canvas, label, (max(0, x-30), max(20, min(canvas.shape[0]-10, y+28))),
                   cv.FONT_HERSHEY_SIMPLEX, .55, color, 2, cv.LINE_AA)
    return canvas
