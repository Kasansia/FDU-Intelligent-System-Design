"""Explicit simulation for repeatable no-camera demos; never claims model inference.

Real ArUco detection processes the generated marker image. Only hand landmarks
are scripted, using the known synthetic board coordinates rather than a detector.
"""
import cv2 as cv
import numpy as np
from hand_tracking import NAMES, EDGES
from instrument_geometry import raw_to_display_xy, InstrumentLayout


def board_image():
    image = np.full((480, 640, 3), (35, 45, 39), np.uint8)
    cv.rectangle(image, (45, 45), (595, 435), (222, 226, 214), -1)
    dictionary = cv.aruco.getPredefinedDictionary(cv.aruco.DICT_4X4_50)
    for i, (x, y) in enumerate(((60, 60), (520, 60), (60, 360), (520, 360))):
        marker = cv.aruco.generateImageMarker(dictionary, i, 60)
        image[y:y+60, x:x+60] = cv.cvtColor(marker, cv.COLOR_GRAY2BGR)
    return image


class SyntheticHandTracker:
    def __init__(self, mirror=True, layout=None):
        self.mirror, self.layout = mirror, layout or InstrumentLayout()
        self.frame_id = 0
        self.started = None

    def process(self, raw_frame, timestamp):
        self.started = timestamp if self.started is None else self.started
        elapsed = timestamp - self.started
        self.frame_id += 1
        cells = list(self.layout.preset.regions.values())
        index = int(elapsed/1.8) % len(cells)
        phase = elapsed % 1.2
        v = (.3 if phase < .3 else .3+(phase-.3)*2 if phase < .5 else
             .7 if phase < .9 else .7-(phase-.9)*2 if phase < 1.1 else .3)
        hands = []
        left_target = np.mean(cells[index], axis=0)
        # Generate both hands in the active region frames, including tilted presets.
        from instrument_geometry import project
        from instrument_preset import frame_transform
        if self.layout.preset.region_frame is not None:
            left_target = project([[(index+.5)/len(cells), .5]], np.linalg.inv(frame_transform(self.layout.preset.region_frame)))[0]
        right_target = (project([[.5, v]], np.linalg.inv(frame_transform(self.layout.preset.strum_frame)))[0]
                        if self.layout.preset.strum_frame is not None else np.mean(self.layout.preset.axis, axis=0)+[0, v-.5])
        for role, (u, y), tid in (('Left', left_target, 1), ('Right', right_target, 2)):
            # Both roles use the index fingertip as their default interaction point.
            offsets = np.array([[0., .10]] + [[(finger-2)*.025, .07-joint*.025]
                               for finger in range(5) for joint in range(4)])
            reference = offsets[8]
            uv = offsets - reference + [u, y]
            if self.layout.flip_u:
                uv[:, 0] = 1 - uv[:, 0]
            raw = uv * [460., 300.] + [89.5, 89.5]
            display = raw_to_display_xy(raw, raw_frame.shape[1], self.mirror)
            screen = np.column_stack((display, np.zeros(21)))
            hands.append({'handedness': role, 'handedness_score': 1., 'confidence': 1., 'palm_confidence': 1.,
                          'track_id': tid, 'bbox_px': [*display.min(axis=0), *display.max(axis=0)],
                          'landmarks_px': screen.tolist(),
                          'landmarks_normalized': (screen/[raw_frame.shape[1], raw_frame.shape[0], raw_frame.shape[1]]).tolist(),
                          'landmarks_world_m': np.zeros((21, 3)).tolist()})
        return (cv.flip(raw_frame, 1) if self.mirror else raw_frame.copy()), {
            'schema_version': 1, 'frame_id': self.frame_id, 'timestamp_unix_s': timestamp,
            'frame_width': raw_frame.shape[1], 'frame_height': raw_frame.shape[0],
            'mirrored': self.mirror, 'inference_ms': 0., 'landmark_names': NAMES,
            'connections': EDGES, 'hands': hands, 'source': 'synthetic',
            'simulation_notice': 'Scripted hands; generated marker image. Not live camera or model inference.'}
