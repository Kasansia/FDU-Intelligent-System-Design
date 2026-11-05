"""Time-based chord selection and strum events. No camera, drawing or audio I/O.

Four-chord/strum concept references SMF-an/MotionMuse; implementation is new.
"""
from dataclasses import dataclass
import math
import time
import numpy as np
from instrument_geometry import CHORDS, InstrumentLayout
from timing import monotonic_s


@dataclass(frozen=True)
class InteractionConfig:
    chord_dwell_s: float = .10
    chord_margin: float = .015
    confidence_min: float = .8
    handedness_min: float = .6
    above_v: float = .45
    below_v: float = .55
    min_speed_uv_s: float = .6
    full_velocity_speed: float = 3.0
    cooldown_s: float = .18
    max_gap_s: float = .25
    max_age_s: float = .35
    max_jump_uv: float = .45
    max_speed_uv_s: float = 12.
    chord_nodes: tuple = (5, 9, 13, 17)
    strum_node: int = 8

    def __post_init__(self):
        values = [v for v in vars(self).values() if isinstance(v, (float, int))]
        if not all(math.isfinite(v) for v in values):
            raise ValueError('Thresholds must be finite')
        if not (0 < self.above_v < .5 < self.below_v < 1):
            raise ValueError('Strum thresholds must surround v=.5')
        if (min(self.chord_dwell_s, self.min_speed_uv_s, self.max_gap_s, self.max_age_s,
                self.max_jump_uv, self.max_speed_uv_s) <= 0 or self.cooldown_s < 0 or
                not 0 <= self.chord_margin < .07 or not 0 <= self.confidence_min <= 1 or
                not 0 <= self.handedness_min <= 1 or self.full_velocity_speed <= self.min_speed_uv_s):
            raise ValueError('Invalid interaction thresholds')
        if not self.chord_nodes or any(i not in range(21) for i in self.chord_nodes) or self.strum_node not in range(21):
            raise ValueError('Landmark indices must be in 0..20')


class InstrumentLogic:
    def __init__(self, layout=None, config=None, clock=monotonic_s):
        self.layout, self.config, self.clock = layout or InstrumentLayout(), config or InteractionConfig(), clock
        self.event_id = 0
        self.last_frame = self.last_time = None
        self.last_event_time = -math.inf
        self.current_chord = None
        self.roles = {}
        self._reset()

    def _reset_strum(self):
        self.previous = self.anchor = None
        self.strum_state = 'unarmed'

    def _reset(self):
        self.candidate = self.candidate_since = self.left_track = None
        self.chord_valid = False
        self._reset_strum()

    def _state(self, reason=None, speed=None):
        return {'current_chord': self.current_chord, 'chord_valid': self.chord_valid,
                'chord_stale': self.current_chord is not None and not self.chord_valid,
                'candidate_chord': self.candidate, 'candidate_since_monotonic_s': self.candidate_since,
                'strum_state': self.strum_state, 'last_event_id': self.event_id,
                'reason': reason, 'strum_speed_uv_s': speed}

    def invalidate(self, reason='camera_unavailable'):
        self._reset()
        self.roles = {}
        return self._state(reason)

    def _point(self, hand, nodes):
        if hand is None:
            return None
        if hand.get('interaction_eligible') is False:
            return None
        try:
            confidence = float(hand['confidence'])
            handedness = float(hand.get('handedness_score', 1.))
            uv = np.asarray(hand['landmarks_instrument_uv'], dtype=np.float64)
            if (not math.isfinite(confidence) or confidence < self.config.confidence_min or
                    not math.isfinite(handedness) or handedness < self.config.handedness_min or
                    uv.shape != (21, 2) or not np.isfinite(uv).all()):
                return None
            return uv[list(nodes)].mean(axis=0)
        except (KeyError, ValueError, TypeError):
            return None

    def _chord(self, hand, now):
        point = self._point(hand, self.config.chord_nodes)
        if point is None:
            self.candidate = self.candidate_since = self.left_track = None
            self.chord_valid = False
            return
        track = hand.get('track_id')
        if track is None or track != self.left_track:
            self.chord_valid = False
            self.candidate = self.candidate_since = None
        self.left_track = track
        raw_chord = self.layout.chord_at(point)
        # A fresh hand may stay in the previous cell's margin; stale cached chords may not.
        if self.chord_valid and raw_chord is not None:
            if self.layout.contains_chord(self.current_chord, point, -self.config.chord_margin):
                self.candidate = self.current_chord
                return
        detected = self.layout.chord_at(point, self.config.chord_margin)
        self.chord_valid = False
        if detected is None:
            self.candidate = self.candidate_since = None
            return
        if detected != self.candidate:
            self.candidate, self.candidate_since = detected, now
        if self.candidate_since is not None and now - self.candidate_since >= self.config.chord_dwell_s - 1e-9:
            self.current_chord, self.chord_valid = detected, True

    def _side(self, v):
        return 'above' if v <= self.config.above_v else 'below' if v >= self.config.below_v else 'crossing'

    def process(self, perception_packet):
        p, c = perception_packet, self.config
        try:
            now, frame, age = float(p['timestamp_monotonic_s']), p['frame_id'], self.clock()
            valid_time = (math.isfinite(now) and isinstance(frame, int) and
                          -.01 <= age - now <= c.max_age_s and
                          (self.last_time is None or now > self.last_time) and
                          (self.last_frame is None or frame > self.last_frame))
        except (KeyError, TypeError, ValueError):
            valid_time = False
        if not valid_time:
            self._reset()
            return self._state('stale_or_out_of_order_frame'), []
        gap = self.last_time is not None and now - self.last_time > c.max_gap_s
        self.last_frame, self.last_time = frame, now
        if gap:
            self._reset()
        if p.get('status') != 'running' or p.get('instrument', {}).get('status') != 'valid':
            self._reset()
            return self._state('board_or_service_unavailable'), []
        hands = p.get('hands', [])
        roles = [h.get('handedness') for h in hands]
        ids = [h.get('track_id') for h in hands]
        if (any(role not in ('Left', 'Right') for role in roles) or len(set(roles)) != len(roles) or
                any(not isinstance(tid, int) for tid in ids) or len(set(ids)) != len(ids)):
            self._reset()
            self.roles = {}
            return self._state('ambiguous_hand_identity'), []
        new_roles = dict(zip(ids, roles))
        if any(tid in self.roles and self.roles[tid] != role for tid, role in new_roles.items()):
            self._reset()
            self.roles = new_roles
            return self._state('hand_identity_changed'), []
        self.roles = new_roles
        by_role = dict(zip(roles, hands))
        self._chord(by_role.get('Left'), now)
        right = by_role.get('Right')
        point = self._point(right, (c.strum_node,))
        if not self.chord_valid or point is None or not self.layout.in_strum_band(point):
            self._reset_strum()
            return self._state('fresh_chord_and_strum_hand_required'), []
        coordinate = self.layout.strum_coordinate(point)
        point = self.layout.strum_motion_point(point)
        if not np.isfinite(point).all() or not math.isfinite(coordinate):
            self._reset_strum()
            return self._state('invalid_region_projection'), []
        tid, side = right['track_id'], self._side(coordinate)
        previous = self.previous
        self.previous = (point, now, tid)
        if previous is None:
            if side != 'crossing':
                self.anchor = (side, coordinate, now)
            self.strum_state = side if self.anchor else 'unarmed'
            return self._state(), []
        distance = float(np.linalg.norm(point - previous[0]))
        dt = now - previous[1]
        if tid != previous[2] or distance > c.max_jump_uv or distance / dt > c.max_speed_uv_s:
            self._reset_strum()
            return self._state('strum_track_changed_or_jump'), []
        self.strum_state = side
        if side == 'crossing':
            return self._state(), []
        anchor = self.anchor
        self.anchor = (side, coordinate, now)
        if anchor is None or side == anchor[0]:
            return self._state(), []
        speed = abs(coordinate - anchor[1]) / (now - anchor[2])
        if speed < c.min_speed_uv_s or now - self.last_event_time < c.cooldown_s - 1e-9:
            return self._state('slow_crossing_or_cooldown', speed), []
        # Linear speed mapping: min_speed->1, full_velocity_speed->127, clipped.
        velocity = int(np.clip(round(1 + 126 * (speed - c.min_speed_uv_s) /
                                     (c.full_velocity_speed - c.min_speed_uv_s)), 1, 127))
        self.event_id += 1
        self.last_event_time = now
        event = {'event_id': self.event_id, 'source_frame_id': frame, 'timestamp_monotonic_s': now,
                 'direction': 'down' if side == 'below' else 'up', 'chord': self.current_chord, 'velocity': velocity}
        return self._state(speed=speed), [event]
