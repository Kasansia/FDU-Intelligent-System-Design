"""Time-based chord selection and strum events. No camera, drawing or audio I/O.

Four-chord/strum concept references SMF-an/MotionMuse; implementation is new.
"""
from dataclasses import dataclass
import math
import time
import numpy as np
from instrument_geometry import InstrumentLayout
from music import Performance, LEGACY_DEGREES, LEGACY_NOTES
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
    chord_nodes: tuple = (8,)
    strum_node: int = 8
    strum_path_enabled: bool = True
    strum_tracking_margin: float = .35
    selection_margin_fraction: float | None = .05

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
        if (type(self.strum_path_enabled) is not bool or not 0 <= self.strum_tracking_margin <= .5 or
                (self.selection_margin_fraction is not None and
                 (not isinstance(self.selection_margin_fraction, (int, float)) or not 0 <= self.selection_margin_fraction <= .25))):
            raise ValueError('Invalid region/trajectory settings')


class InstrumentLogic:
    def __init__(self, layout=None, config=None, clock=monotonic_s, performance=None):
        self.layout, self.config, self.clock = layout or InstrumentLayout(), config or InteractionConfig(), clock
        self.performance = performance or Performance()
        self.event_id = 0
        self.last_frame = self.last_time = None
        self.last_event_time = -math.inf
        self.current_chord = None
        self.roles = {}
        self._reset()

    def _reset_strum(self):
        self.previous = self.anchor = None
        self.axis_side = None
        self.axis_contact = False
        self.crossed_axis = False
        self.strum_state = 'unarmed'

    def _reset(self):
        self.candidate = self.candidate_since = self.left_track = None
        self.chord_valid = False
        self.chord_point_uv = self.chord_reason = None
        self.chord_dwell_progress = 0.
        self.strum_point_uv = self.strum_point_local = self.strum_reason = None
        self.strum_in_region = False
        self._reset_strum()

    def selection(self, key):
        if key is None:
            return dict(degree=None, label=None, chord=None, notes_midi=[])
        legacy = key in LEGACY_DEGREES
        voice = self.performance.voice(LEGACY_DEGREES[key] if legacy else int(key))
        if legacy and self.performance.mode == 'chord' and self.performance.tonic == 'C':
            voice['notes_midi'] = list(LEGACY_NOTES[key])
        return voice

    def set_performance(self, performance):
        if performance == self.performance:
            return
        self.performance = performance
        self.current_chord = None
        self.last_event_time = -math.inf
        self.invalidate('performance_changed')

    def _state(self, reason=None, speed=None):
        selected, candidate = self.selection(self.current_chord), self.selection(self.candidate)
        chord_mode = self.performance.mode == 'chord'
        return {'current_chord': selected['chord'], 'chord_valid': self.chord_valid and chord_mode,
                'chord_stale': selected['chord'] is not None and not self.chord_valid,
                'candidate_chord': candidate['chord'], 'candidate_since_monotonic_s': self.candidate_since,
                'selected_degree': selected['degree'], 'selected_label': selected['label'],
                'selection_valid': self.chord_valid, 'candidate_degree': candidate['degree'],
                'candidate_label': candidate['label'], 'selection_point_uv': self.chord_point_uv,
                'selection_reason': self.chord_reason or reason,
                'selection_dwell_progress': self.chord_dwell_progress,
                'chord_point_uv': self.chord_point_uv, 'chord_nodes': list(self.config.chord_nodes),
                'chord_reason': self.chord_reason or reason,
                'chord_dwell_progress': self.chord_dwell_progress,
                'strum_point_uv': self.strum_point_uv, 'strum_point_local': self.strum_point_local,
                'strum_in_region': self.strum_in_region,
                'strum_reason': self.strum_reason or reason or ('strum_waiting_crossing' if self.anchor else 'strum_waiting_start_side'),
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
        self.chord_point_uv = point.tolist() if point is not None else None
        self.chord_dwell_progress = 0.
        if point is None:
            self.candidate = self.candidate_since = self.left_track = None
            self.chord_valid = False
            self.chord_reason = ('left_hand_missing' if hand is None else
                                 'left_hand_projection_unavailable' if hand.get('interaction_eligible') is False else
                                 'left_hand_unreliable')
            return
        track = hand.get('track_id')
        if track is None or track != self.left_track:
            self.chord_valid = False
            self.candidate = self.candidate_since = None
        self.left_track = track
        raw_chord = self.layout.chord_at(point)
        margin = lambda key: self.layout.selection_margin(key, self.config.chord_margin, self.config.selection_margin_fraction)
        # A fresh hand may stay in the previous cell's margin; stale cached chords may not.
        if self.chord_valid and raw_chord is not None:
            if self.layout.contains_chord(self.current_chord, point, -margin(self.current_chord)):
                self.candidate = self.current_chord
                self.chord_reason, self.chord_dwell_progress = 'selected', 1.
                return
        detected = self.layout.chord_at(point, margin(raw_chord))
        self.chord_valid = False
        if detected is None:
            self.candidate = self.candidate_since = None
            self.chord_reason = 'chord_boundary' if raw_chord is not None else 'outside_chord_regions'
            return
        if detected != self.candidate:
            self.candidate, self.candidate_since = detected, now
        self.chord_reason = 'dwelling'
        self.chord_dwell_progress = min(1., max(0., (now - self.candidate_since) / self.config.chord_dwell_s))
        if self.candidate_since is not None and now - self.candidate_since >= self.config.chord_dwell_s - 1e-9:
            self.current_chord, self.chord_valid = detected, True
            self.chord_reason, self.chord_dwell_progress = 'selected', 1.

    def _side(self, v):
        return 'above' if v <= self.config.above_v else 'below' if v >= self.config.below_v else 'crossing'

    def _observe_strum_axis(self, coordinate, crossed):
        if coordinate == .5:
            self.axis_contact = self.axis_contact or crossed
            return
        side = coordinate > .5
        if self.axis_side is not None and side != self.axis_side:
            # The latest change of side must cross the finite axis. A return across
            # the axis before reaching the opposite threshold undoes an earlier sweep.
            self.crossed_axis = crossed or self.axis_contact
        self.axis_side = side
        self.axis_contact = False

    def process(self, perception_packet):
        p, c = perception_packet, self.config
        self.strum_point_uv = self.strum_point_local = self.strum_reason = None
        self.strum_in_region = False
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
        if point is not None:
            self.strum_point_uv = point.tolist()
            local_point = self.layout.strum_motion_point(point)
            self.strum_point_local = local_point.tolist() if np.isfinite(local_point).all() else None
            self.strum_in_region = self.layout.in_strum_band(point)
        if not self.chord_valid or point is None:
            self.strum_reason = ('selection_not_ready' if not self.chord_valid else
                                 'right_hand_missing' if right is None else 'right_hand_unreliable')
            self._reset_strum()
            return self._state('fresh_chord_and_strum_hand_required'), []
        in_zone = (self.layout.in_strum_tracking_zone(point, c.strum_tracking_margin) if c.strum_path_enabled
                   else self.strum_in_region)
        if not in_zone:
            self.strum_reason = 'outside_strum_zone'
            self._reset_strum()
            return self._state('fresh_chord_and_strum_hand_required'), []
        coordinate = self.layout.strum_coordinate(point)
        board_point = point.copy()
        point = self.layout.strum_motion_point(point)
        if not np.isfinite(point).all() or not math.isfinite(coordinate):
            self._reset_strum()
            return self._state('invalid_region_projection'), []
        tid, side = right['track_id'], self._side(coordinate)
        previous = self.previous
        self.previous = (point, now, tid, board_point)
        if previous is None:
            self._observe_strum_axis(coordinate, False)
            if side != 'crossing':
                self.anchor = (side, coordinate, now, board_point)
            self.strum_state = side if self.anchor else 'unarmed'
            return self._state(), []
        distance = float(np.linalg.norm(point - previous[0]))
        dt = now - previous[1]
        crossed = c.strum_path_enabled and self.layout.strum_path_crosses(previous[3], board_point)
        if tid != previous[2] or (distance > c.max_jump_uv and not crossed) or distance / dt > c.max_speed_uv_s:
            self.strum_reason = 'strum_identity_changed' if tid != previous[2] else 'strum_motion_outlier'
            self._reset_strum()
            return self._state('strum_track_changed_or_jump'), []
        self._observe_strum_axis(coordinate, crossed)
        self.strum_state = side
        if side == 'crossing':
            return self._state(), []
        anchor = self.anchor
        self.anchor = (side, coordinate, now, board_point)
        if anchor is None or side == anchor[0]:
            self.crossed_axis = False
            return self._state(), []
        crossed_axis, self.crossed_axis = self.crossed_axis, False
        if c.strum_path_enabled and not crossed_axis:
            self.strum_reason = 'strum_missed_axis'
            return self._state(), []
        speed = abs(coordinate - anchor[1]) / (now - anchor[2])
        if speed < c.min_speed_uv_s or now - self.last_event_time < c.cooldown_s - 1e-9:
            self.strum_reason = 'strum_too_slow' if speed < c.min_speed_uv_s else 'strum_cooldown'
            return self._state('slow_crossing_or_cooldown', speed), []
        # Linear speed mapping: min_speed->1, full_velocity_speed->127, clipped.
        velocity = int(np.clip(round(1 + 126 * (speed - c.min_speed_uv_s) /
                                     (c.full_velocity_speed - c.min_speed_uv_s)), 1, 127))
        self.event_id += 1
        self.last_event_time = now
        self.strum_reason = 'strum_triggered'
        event = {'event_id': self.event_id, 'source_frame_id': frame, 'timestamp_monotonic_s': now,
                 'direction': 'down' if side == 'below' else 'up', 'velocity': velocity,
                 **self.selection(self.current_chord), 'mode': self.performance.mode,
                 'tonic': self.performance.tonic, 'duration_ms': 900,
                 'performance_revision': self.performance.revision}
        return self._state(speed=speed), [event]
