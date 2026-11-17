"""Single-frame orchestration; camera ownership stays in app.Service."""
import time
from collections import deque
from dataclasses import asdict
import cv2 as cv
import numpy as np
from aruco_tracking import ArucoBoardTracker
from hand_tracking import HandTracker, annotate
from instrument_geometry import (InstrumentLayout, fuse_hands, overlay_polygons,
                                 raw_to_display_xy, instrument_to_display_xy)
from instrument_logic import InstrumentLogic
from timing import monotonic_s


class PerceptionPipeline:
    def __init__(self, hands=None, board=None, layout=None, logic=None, audio=None):
        self.hands = hands if hands is not None else HandTracker()
        self.board = board if board is not None else ArucoBoardTracker(learn_legacy=True)
        self.layout = layout or InstrumentLayout()
        self.logic = logic if logic is not None else InstrumentLogic(self.layout)
        self.audio = audio
        self.recent_events = deque(maxlen=32)

    def configuration(self):
        return dict(board=self.board.board.to_dict() if self.board.board else None,
                    tracking=asdict(self.board.config), preset=self.layout.preset.to_dict(),
                    flip_u=self.layout.flip_u, interaction=asdict(self.logic.config),
                    performance=self.logic.performance.configuration())

    def process(self, raw_frame, timestamp_unix_s=None, timestamp_monotonic_s=None, interaction_enabled=True):
        captured = time.time() if timestamp_unix_s is None else timestamp_unix_s
        mono = monotonic_s() if timestamp_monotonic_s is None else timestamp_monotonic_s
        started = time.perf_counter()
        display, packet = self.hands.process(raw_frame, captured)
        previous_pose = self.board.export_state()
        instrument = self.board.process(raw_frame, mono)
        instrument['source_frame_id'] = packet['frame_id']
        fuse_hands(packet, instrument)
        packet.update(timestamp_monotonic_s=mono, status='running', layout=self.layout.regions(),
                      performance=self.logic.performance.snapshot(),
                      tracking_previous_state=previous_pose,
                      vision_total_ms=round((time.perf_counter() - started) * 1000, 3))
        if interaction_enabled:
            packet['interaction'], packet['events'] = self.logic.process(packet)
        else:
            packet['interaction'], packet['events'] = self.logic.invalidate('calibration_in_progress'), []
        for event in packet['events']:
            self.recent_events.append(event)
            if self.audio is not None:
                self.audio.submit(event)
        packet['recent_events'] = list(self.recent_events)
        packet['audio'] = self.audio.snapshot() if self.audio is not None else {'status': 'disabled'}
        packet['audio_status'] = packet['audio']['status']
        return display, packet


def annotate_instrument(display, packet, layout):
    canvas = annotate(display, packet)
    board = packet['instrument']
    state = packet.get('interaction', {})
    green, amber, muted = (176, 214, 88), (78, 174, 243), (130, 130, 130)

    def integer_points(points):
        # Projective points close to a horizon can be huge; protect OpenCV int32 conversion.
        return np.rint(np.clip(points, -1e6, 1e6)).astype(np.int32)

    for marker in board['markers']:
        points = raw_to_display_xy(marker['corners_px_raw'], packet['frame_width'], packet['mirrored'])
        points = integer_points(points)
        cv.polylines(canvas, [points], True, green, 1, cv.LINE_AA)
        cv.putText(canvas, str(marker['id']), tuple(points[0]), cv.FONT_HERSHEY_SIMPLEX, .5, amber, 2)
    shapes = overlay_polygons(packet, layout)
    # Fill the actual hit-test polygon, leaving the same-camera landmarks visible.
    tint = canvas.copy()
    for name, points in shapes.items():
        if points is None or name not in layout.preset.regions:
            continue
        active = ((name == str(state.get('selected_degree')) or name == state.get('current_chord'))
                  and state.get('selection_valid', state.get('chord_valid')))
        color = muted if board['status'] != 'valid' else amber if active else green
        cv.fillPoly(tint, [integer_points(points)], color)
    canvas = cv.addWeighted(tint, .22, canvas, .78, 0)
    for name, points in shapes.items():
        if points is None:
            continue
        polygon = integer_points(points)
        active = ((name == str(state.get('selected_degree')) or name == state.get('current_chord'))
                  and state.get('selection_valid', state.get('chord_valid')))
        color = muted if board['status'] != 'valid' else amber if active or name in ('strum', 'axis') else green
        cv.polylines(canvas, [polygon], name != 'axis', color, 2, cv.LINE_AA)
        if name in layout.preset.regions:
            voice = next((v for v in packet.get('performance', {}).get('voices', []) if str(v['degree']) == name), None)
            label = f"{name}:{voice['label']}" if voice else name
            available = max(14., np.linalg.norm(np.asarray(points[1])-points[0])*.9)
            scale = min(.45, available/cv.getTextSize(label, cv.FONT_HERSHEY_SIMPLEX, 1., 1)[0][0])
            size = cv.getTextSize(label, cv.FONT_HERSHEY_SIMPLEX, scale, 1)[0]
            origin = np.mean(points, axis=0)-[size[0]/2, -size[1]/2]
            cv.putText(canvas, label, tuple(integer_points(origin)),
                       cv.FONT_HERSHEY_SIMPLEX, scale, color, 1, cv.LINE_AA)
    if state.get('chord_point_uv') is not None:
        point = instrument_to_display_xy([state['chord_point_uv']], board['homography_instrument_to_camera'],
                                         packet['frame_width'], packet['mirrored'])
        if point is not None:
            x, y = integer_points(point[0])
            if 0 <= x < canvas.shape[1] and 0 <= y < canvas.shape[0]:
                color = amber if state.get('selection_valid', state.get('chord_valid')) else green
                cv.circle(canvas, (x, y), 10, (15, 25, 20), 4, cv.LINE_AA)
                cv.circle(canvas, (x, y), 8, color, 2, cv.LINE_AA)
                cv.putText(canvas, 'SELECT', (max(0, min(x+12, canvas.shape[1]-70)), max(16, y-12)),
                           cv.FONT_HERSHEY_SIMPLEX, .4, color, 1, cv.LINE_AA)
    if state.get('strum_point_uv') is not None:
        point = instrument_to_display_xy([state['strum_point_uv']], board['homography_instrument_to_camera'],
                                         packet['frame_width'], packet['mirrored'])
        if point is not None:
            x, y = integer_points(point[0])
            if 0 <= x < canvas.shape[1] and 0 <= y < canvas.shape[0]:
                cv.circle(canvas, (x, y), 10, (15, 25, 20), 4, cv.LINE_AA)
                cv.circle(canvas, (x, y), 8, amber, 2, cv.LINE_AA)
                cv.putText(canvas, 'STRUM', (max(0, min(x+12, canvas.shape[1]-70)), max(16, y-12)),
                           cv.FONT_HERSHEY_SIMPLEX, .4, amber, 1, cv.LINE_AA)
    performance = packet.get('performance', {})
    label = f"Board: {board['status']} | {performance.get('mode', 'chord')} {performance.get('tonic', 'C')} | {state.get('selected_label') or '-'}"
    cv.putText(canvas, label, (12, 24), cv.FONT_HERSHEY_SIMPLEX, .55, (255, 255, 255), 2, cv.LINE_AA)
    if packet.get('source') == 'synthetic':
        cv.putText(canvas, 'SIMULATION / scripted hands', (15, canvas.shape[0]-12),
                   cv.FONT_HERSHEY_SIMPLEX, .45, (180, 220, 200), 1, cv.LINE_AA)
    return canvas
