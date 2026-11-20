"""Bounded frozen-frame calibration sessions and atomic, non-overwriting saves."""
import base64
from collections import Counter
from copy import deepcopy
import json
import os
from pathlib import Path
import secrets
import tempfile
import threading
import time
import cv2 as cv
import numpy as np
from aruco_tracking import ArucoBoardTracker
from board_definition import BoardDefinition, polygon, safe_name
from instrument_geometry import InstrumentLayout, project, raw_to_display_xy
from instrument_preset import InstrumentPreset, frame_transform
from music import Performance


def calibrated_preset_path(board, board_path):
    """Only a safe companion name, never a client-supplied filesystem path."""
    name = board.metadata.get('calibration_preset')
    return None if name is None else Path(board_path).parent.parent/'presets'/(safe_name(name)+'.json')


def region_preset(name, chord, strum):
    """Seven degree cells and a directed center line in region-local coordinates."""
    strum_inverse = np.linalg.inv(frame_transform(strum))
    axis = project([[0, .5], [1, .5]], strum_inverse).tolist()
    return InstrumentPreset.from_frames(name, np.asarray(chord).tolist(), np.asarray(strum).tolist(), axis,
                                        strum_frame=np.asarray(strum).tolist())


def encode_preview(image):
    ok, encoded = cv.imencode('.jpg', image, [cv.IMWRITE_JPEG_QUALITY, 92])
    if not ok:
        raise ValueError('Cannot encode calibration frame')
    return base64.b64encode(encoded).decode('ascii')


class CalibrationSessions:
    def __init__(self, directory, clock=time.monotonic, ttl_s=600):
        self.directory, self.clock, self.ttl_s = Path(directory), clock, ttl_s
        self.lock = threading.RLock()
        self.session = None  # At most one frozen frame in memory.

    def _suggest_board_id(self):
        directories = (self.directory, self.directory.parent/'presets')
        index, name = 1, 'custom_board'
        while any(os.path.lexists(directory/(name+'.json')) for directory in directories):
            index += 1
            name = f'custom_board_{index}'
        return name

    def capture(self, raw, packet):
        with self.lock:
            if raw is None or packet.get('status') != 'running':
                raise ValueError('Camera is not running; capture a fresh frame first')
            markers = deepcopy(packet['instrument']['markers'])
            counts = Counter(m['id'] for m in markers)
            if any(n != 1 for n in counts.values()):
                raise ValueError('Duplicate marker IDs in frozen frame; use distinct printed IDs')
            if len(markers) < 2:
                raise ValueError('Calibration requires at least two well-separated visible markers')
            for marker in markers:
                p = polygon(marker['corners_px_raw'], 'detected marker', convex=True)
                if np.linalg.norm(np.roll(p, -1, axis=0)-p, axis=1).min() < 12:
                    raise ValueError('Marker too small; move the camera closer')
                x, y, w, h = cv.boundingRect(np.float32(p))
                patch = raw[max(0, y):y+h, max(0, x):x+w]
                if patch.size == 0 or patch.std() < 25 or cv.Laplacian(cv.cvtColor(patch, cv.COLOR_BGR2GRAY), cv.CV_64F).var() < 30:
                    raise ValueError('Marker image lacks contrast or sharpness; recapture')
            sid = secrets.token_urlsafe(24)
            self.session = dict(id=sid, created=self.clock(), raw=raw.copy(), markers=markers,
                                frame_id=packet['frame_id'], timestamp_unix_s=packet['timestamp_unix_s'],
                                dictionary=packet['instrument']['dictionary'], mirrored=packet['mirrored'],
                                preview=None)
            display = cv.flip(raw, 1) if packet['mirrored'] else raw
            return dict(session_id=sid, frame_id=packet['frame_id'], frame_width=raw.shape[1],
                        frame_height=raw.shape[0], mirrored=packet['mirrored'],
                        visible_ids=sorted(counts), image_base64=encode_preview(display), expires_in_s=self.ttl_s,
                        suggested_board_id=self._suggest_board_id())

    def _get(self, sid):
        if self.session is None or not secrets.compare_digest(str(sid), self.session['id']):
            raise ValueError('Unknown or replaced calibration session; capture again')
        if self.clock()-self.session['created'] > self.ttl_s:
            self.session = None
            raise ValueError('Calibration session expired; capture again')
        return self.session

    def active(self):
        with self.lock:
            if self.session is not None and self.clock()-self.session['created'] > self.ttl_s:
                self.session = None
            return self.session is not None

    def preview(self, sid, points_raw, board_id, layout, regions_raw=None, performance=None):
        with self.lock:
            s = self._get(sid)
            s['preview'] = None
            safe_name(board_id)
            if (self.directory/(board_id+'.json')).exists():
                raise ValueError('Board name already exists; choose a new name')
            height, width = s['raw'].shape[:2]
            def quad(value, label):
                p = polygon(value, label, convex=True)
                if len(p) != 4 or abs(cv.contourArea(np.float32(p))) < 400:
                    raise ValueError(f'{label}: select four corners around an area of at least 400 pixels')
                if np.any(p < 0) or np.any(p[:, 0] > width-1) or np.any(p[:, 1] > height-1):
                    raise ValueError(f'{label}: point is outside the frozen image')
                edges = np.roll(p, -1, axis=0)-p
                lengths = np.linalg.norm(edges, axis=1)
                crosses = edges[:, 0]*np.roll(edges, -1, axis=0)[:, 1]-edges[:, 1]*np.roll(edges, -1, axis=0)[:, 0]
                if lengths.min() < 20 or np.min(np.abs(crosses)/(lengths*np.roll(lengths, -1))) < .04:
                    raise ValueError(f'{label}: region is too narrow or nearly collinear')
                return p
            selected = None
            if regions_raw is not None:
                if not isinstance(regions_raw, dict) or set(regions_raw) != {'chord', 'strum'} or points_raw is not None:
                    raise ValueError('Supply exactly chord and strum regions, without reference points')
                selected = {key: quad(regions_raw[key], key) for key in ('chord', 'strum')}
                if cv.intersectConvexConvex(np.float32(selected['chord']), np.float32(selected['strum']))[0] > 1e-4:
                    raise ValueError('Chord and strum regions must not overlap')
                # Keep physical marker winding positive even on a mirrored canvas.
                p = selected['chord'].copy()
                if cv.contourArea(np.float32(p), oriented=True) < 0:
                    p = p[[1, 0, 3, 2]]
                if (self.directory.parent/'presets'/(board_id+'.json')).exists():
                    raise ValueError('Preset name already exists; choose a new name')
            else:
                p = quad(points_raw, 'reference points')
                if cv.contourArea(np.float32(p), oriented=True) < 0:
                    raise ValueError('Choose physical TL → TR → BR → BL reference points')
            unit = np.float32([[0, 0], [1, 0], [1, 1], [0, 1]])
            H = cv.getPerspectiveTransform(np.float32(p), unit)
            preset, outline = None, None
            if selected is not None:
                regions_uv = {key: project(value, H) for key, value in selected.items()}
                if any(value is None for value in regions_uv.values()):
                    raise ValueError('Selected regions cross the projective horizon')
                preset = region_preset(board_id, regions_uv['chord'], regions_uv['strum'])
                layout = InstrumentLayout(preset=preset)
                outline = cv.convexHull(np.float32(np.vstack(list(regions_uv.values())))).reshape(-1, 2).tolist()
            mapped = {}
            for marker in s['markers']:
                uv = project(marker['corners_px_raw'], H)
                if uv is None:
                    raise ValueError('Reference plane has an unstable projective horizon')
                mapped[marker['id']] = {'corners_uv': uv.tolist()}
            metadata = {
                'method': 'frozen_frame_two_regions' if selected is not None else 'frozen_frame_four_reference_points', 'source_frame_id': s['frame_id'],
                'timestamp_unix_s': s['timestamp_unix_s'], 'reference_points_px_raw': p.tolist(),
                'coordinate_note': 'Projective UV; no metric length or 3D contact claim'}
            if preset is not None:
                metadata.update(calibration_preset=preset.preset_id, regions_px_raw={k: v.tolist() for k, v in selected.items()})
            board = BoardDefinition(board_id, mapped, dictionary=s['dictionary'], outline_uv=outline, metadata=metadata)
            class FrozenDetector:
                def detectMarkers(self, unused):
                    return ([np.float32(m['corners_px_raw']).reshape(1, 4, 2) for m in s['markers']],
                            np.int32([[m['id']] for m in s['markers']]), [])
            result = ArucoBoardTracker(board=board, detector=FrozenDetector()).process(s['raw'], 0.)
            if result['status'] != 'valid':
                raise ValueError('Calibration quality insufficient: '+result['reason'])
            display = cv.flip(s['raw'], 1) if s['mirrored'] else s['raw'].copy()
            regions = layout.regions()
            shapes = dict(regions['degree_regions'] or regions['chords'], strum=regions['strum'], axis=regions['axis'])
            if selected is None:
                shapes['reference'] = unit.tolist()
            inverse = np.linalg.inv(H)
            for name, uv in shapes.items():
                raw = project(uv, inverse)
                if raw is None:
                    raise ValueError('Preset lies across the projective horizon')
                xy = np.rint(np.clip(raw_to_display_xy(raw, width, s['mirrored']), -1e6, 1e6)).astype(np.int32)
                color = (78, 174, 243) if name in ('strum', 'axis') else (50, 200, 120)
                cv.polylines(display, [xy], name != 'axis', color, 2, cv.LINE_AA)
                label = f"{name}:{(performance or Performance()).voice(int(name))['label']}" if name.isdigit() else name
                cv.putText(display, label, tuple(xy[0]), cv.FONT_HERSHEY_SIMPLEX, .4, color, 1)
            token = secrets.token_urlsafe(24)
            s['preview'] = dict(token=token, board=board, preset=preset)
            return dict(session_id=sid, preview_token=token, board=board.to_dict(), pose_quality=result['pose_quality'],
                        preset=None if preset is None else preset.to_dict(),
                        image_base64=encode_preview(display))

    def save(self, sid, token):
        with self.lock:
            s = self._get(sid)
            preview = s['preview']
            if preview is None or not secrets.compare_digest(str(token), preview['token']):
                raise ValueError('Preview the current selection before saving')
            board = preview['board']
            target = self.directory/(safe_name(board.board_id)+'.json')
            preset = preview['preset']
            preset_path = calibrated_preset_path(board, target) if preset is not None else None
            published_preset = False
            try:
                if preset_path is not None:
                    self._publish(preset_path, preset.to_dict())
                    published_preset = True
                # Publish the board last so it never references a missing preset.
                self._publish(target, board.to_dict())
            except FileExistsError as exc:
                if published_preset:
                    preset_path.unlink()
                raise ValueError('Board or preset name already exists; choose a new name') from exc
            except OSError:
                if published_preset:
                    preset_path.unlink()
                raise
            self.session = None
            return board, target

    @staticmethod
    def _publish(target, value):
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=target.parent, suffix='.tmp', delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, target)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def cancel(self, sid):
        with self.lock:
            self._get(sid)
            self.session = None
