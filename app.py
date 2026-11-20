"""Single-camera MotionMuse service, HTTP dashboard and asynchronous recording."""
import argparse
import base64
import ipaddress
import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
from uuid import uuid4
import cv2 as cv
from hand_tracking import ROOT, HandTracker
from pipeline import PerceptionPipeline, annotate_instrument
from instrument_geometry import InstrumentLayout, legacy_packet
from aruco_tracking import ArucoBoardTracker, TrackingConfig
from board_definition import BoardDefinition
from board_calibration import CalibrationSessions, calibrated_preset_path
from instrument_preset import InstrumentPreset
from instrument_logic import InstrumentLogic, InteractionConfig
from audio_engine import AudioEngine, MockAudioBackend, SoundDeviceBackend
from camera_sources import cameras, open_camera
from recording import Recorder
from timing import monotonic_s
from music import Performance

LOG = logging.getLogger('motionmuse')


class Service:
    def __init__(self, args):
        self.args = args
        self.service_instance_id = uuid4().hex
        self.lock = threading.RLock()
        self.record_control = threading.Lock()
        self.pipeline_lock = threading.RLock()
        self.calibration = CalibrationSessions(ROOT/'config/boards')
        self.calibrating = False
        self.layout_error = None
        self.raw_frame = None
        self.stop = threading.Event()
        self.packet = {'schema_version': 3, 'hands': [], 'status': 'starting',
                       'instrument': {'status': 'lost', 'reason': 'starting', 'visible_ids': []},
                       'interaction': {'current_chord': None, 'chord_valid': False},
                       'events': [], 'recent_events': [], 'audio_status': 'starting'}
        self.jpeg = None
        self.backend_name = None
        self.recording = self.last_recording = self.recording_error = None
        self.audio = self.pipeline = None
        self.device_list = [] if args.demo or args.video else cameras()
        selected = next((c for c in self.device_list if 'c270' in c['name'].lower()), None)
        selected = selected or next((c for c in self.device_list if 'ir camera' not in c['name'].lower()), None)
        self.index = args.camera if args.camera is not None else (selected['index'] if selected else 0)
        self.camera_name = ('Synthetic demo' if args.demo else str(args.video) if args.video else
                            next((c['name'] for c in self.device_list if c['index'] == self.index), str(self.index)))
        self.worker = threading.Thread(target=self.run, name='single-camera-worker', daemon=True)

    def set_recording(self, enabled, raw_frames=None):
        # Serialize control requests, but drain files outside the snapshot/camera lock.
        with self.record_control:
            with self.lock:
                old = None
                if self.recording is not None and (not enabled or self.recording.stop.is_set() or self.recording.error):
                    old, self.recording = self.recording, None
            if old is not None:
                old.close()
                with self.lock:
                    self.recording_error = old.error
            with self.lock:
                if enabled and self.recording is None:
                    self.recording = Recorder(ROOT / 'outputs', raw_frames=self.args.record_video if raw_frames is None else raw_frames,
                                              metadata={'source': self.camera_name, 'schema_version': 3})
                    self.last_recording = str(self.recording.folder)
                    self.recording_error = None

    def latest(self, schema_version=2):
        with self.lock:
            result = dict(self.packet, recording=self.recording is not None and not self.recording.stop.is_set(),
                          recording_error=self.recording.error if self.recording else self.recording_error,
                          last_recording=self.last_recording, camera_name=self.camera_name,
                          camera_index=self.index, camera_backend=self.backend_name, available_cameras=self.device_list)
            result['recording_details'] = self.recording.snapshot() if self.recording else None
            result['calibrating'] = self.calibrating
            result['layout_error'] = self.layout_error
            result['recording_default_raw_frames'] = self.args.record_video
            if self.audio is not None:
                result['audio'] = self.audio.snapshot()
                result['audio_status'] = result['audio']['status']
            if schema_version == 2:
                return legacy_packet(result)
            result['service_instance_id'] = self.service_instance_id
            return result

    def snapshot(self):
        with self.lock:
            return self.latest(schema_version=3), self.jpeg

    def publish(self, display, packet, raw_frame=None):
        ok, encoded = cv.imencode('.jpg', display, [cv.IMWRITE_JPEG_QUALITY, 85])
        if not ok:
            raise RuntimeError('JPEG encoding failed')
        # Strict serialization guards recorded and HTTP data alike.
        json.dumps(packet, allow_nan=False)
        with self.lock:
            self.packet, self.jpeg = packet, encoded.tobytes()
            self.raw_frame = None if raw_frame is None else raw_frame.copy()
            if self.recording is not None:
                self.recording.submit(packet, raw_frame, self.pipeline.configuration() if self.pipeline else None)

    def fail_frame(self, message):
        with self.lock:
            self.packet = dict(self.packet, status='error', error=message, hands=[], events=[],
                               instrument={'status': 'lost', 'reason': 'camera_unavailable', 'visible_ids': [],
                                           'markers': [], 'homography_camera_to_instrument': None,
                                           'homography_instrument_to_camera': None, 'homography_age_s': None},
                               interaction=self.pipeline.logic.invalidate() if self.pipeline else
                               {'current_chord': None, 'chord_valid': False})
            self.jpeg = None
            self.raw_frame = None
        if self.pipeline is not None:
            self.pipeline.board.reset()

    def run(self):
        cap = None
        try:
            cv.setNumThreads(4)
            board = BoardDefinition.load(self.args.board) if self.args.board else None
            companion = calibrated_preset_path(board, self.args.board) if board else None
            preset_path = self.args.preset or companion
            preset = InstrumentPreset.load(preset_path) if preset_path else InstrumentPreset.default()
            try:
                preset = preset.for_live()
            except ValueError as exc:
                self.layout_error = str(exc)
            layout = InstrumentLayout(flip_u=self.args.layout == 'flip-u', preset=preset)
            tracking_data = {}
            if self.args.tracking_config:
                with open(self.args.tracking_config, encoding='utf-8') as file:
                    tracking_data.update(json.load(file))
            if self.args.board_hold is not None:
                tracking_data['hold_s'] = self.args.board_hold
            tracking = TrackingConfig(**tracking_data)
            board_tracker = ArucoBoardTracker(board=board, learn_legacy=True, config=tracking)
            config = self.interaction_config(preset)
            if self.args.demo:
                from synthetic_input import SyntheticHandTracker, board_image
                tracker = SyntheticHandTracker(mirror=not self.args.no_mirror, layout=layout)
                frame = board_image()
                self.backend_name = 'synthetic'
            else:
                tracker = HandTracker(self.args.palm_threshold, self.args.hand_threshold, mirror=not self.args.no_mirror)
                cap, frame = open_camera(self.index, self.camera_name, self.args.backend,
                                         self.args.width, self.args.height, self.args.gstreamer, self.args.video)
                self.backend_name = cap.getBackendName()
            audio_mode = self.args.audio or ('mock' if self.args.demo else 'sounddevice')
            device = self.args.audio_device
            device = int(device) if device and device.isdigit() else device
            backend = MockAudioBackend() if audio_mode in ('mock', 'off') else SoundDeviceBackend(device)
            self.audio = AudioEngine(backend, enabled=audio_mode != 'off')
            self.pipeline = PerceptionPipeline(hands=tracker, board=board_tracker, layout=layout, audio=self.audio,
                                               logic=InstrumentLogic(layout, config))
            previous, fps, failed = time.perf_counter(), 0., 0
            deadline = time.monotonic() + self.args.duration if self.args.duration else None
            first = True
            while not self.stop.is_set() and (deadline is None or time.monotonic() < deadline):
                if not self.args.demo and not first:
                    ok, frame = cap.read()
                    if not ok or frame is None:
                        if self.args.video:
                            break
                        failed += 1
                        self.fail_frame('Camera frame unavailable')
                        if failed >= 30:
                            raise RuntimeError('Camera disconnected; restart after reconnecting.')
                        self.stop.wait(.1)
                        continue
                first, failed = False, 0
                captured, mono = time.time(), monotonic_s()
                with self.pipeline_lock:
                    self.calibrating = self.calibration.active()
                    display, packet = self.pipeline.process(frame, captured, mono, interaction_enabled=not self.calibrating and not self.layout_error)
                    if self.layout_error:
                        packet['interaction'] = self.pipeline.logic.invalidate('layout_requires_calibration')
                    packet.setdefault('source', 'video' if self.args.video else 'camera')
                    if audio_mode == 'mock' and self.audio.status == 'mock':
                        backend.pump()
                        packet['audio'] = self.audio.snapshot()
                        packet['audio_status'] = packet['audio']['status']
                    now = time.perf_counter()
                    instant = 1/max(now-previous, .0001)
                    fps = instant if not fps else .9*fps + .1*instant
                    previous = now
                    packet.update(fps=round(fps, 1))
                    self.publish(annotate_instrument(display, packet, self.pipeline.layout), packet, frame)
                if self.args.demo:
                    self.stop.wait(1/30)
            with self.lock:
                self.packet = dict(self.packet, status='stopped', hands=[], events=[],
                                   interaction=self.pipeline.logic.invalidate('service_stopped'))
                self.jpeg = None
        except Exception as exc:
            LOG.exception('Camera worker failed')
            self.fail_frame(str(exc))
        finally:
            if cap is not None:
                cap.release()
            if self.audio is not None:
                self.audio.close()
            self.set_recording(False)

    def interaction_config(self, preset):
        data = dict(preset.interaction)
        if self.args.interaction_config:
            with open(self.args.interaction_config, encoding='utf-8') as file:
                data.update(json.load(file))
        return InteractionConfig(**data)

    def performance_request(self, body=None):
        with self.pipeline_lock, self.lock:
            if self.pipeline is None or self.stop.is_set() or self.packet['status'] != 'running':
                return None
            current = self.pipeline.logic.performance
            if body is not None:
                if self.calibration.active():
                    raise ValueError('标定期间不能切换模式或调性')
                if set(body) != {'mode', 'tonic'}:
                    raise ValueError('Expected mode and tonic')
                updated = Performance(body['mode'], body['tonic'], current.revision+1)
                if (updated.mode, updated.tonic) != (current.mode, current.tonic):
                    self.pipeline.logic.set_performance(updated)
                    if self.audio is not None:
                        self.audio.set_performance_revision(updated.revision)
                    current = updated
            # Publish settings with the next matching image; old snapshots keep their revision.
            return dict(current.snapshot(), service_instance_id=self.service_instance_id)

    def calibration_request(self, action, body):
        with self.pipeline_lock:
            if action == 'capture':
                with self.lock:
                    if monotonic_s()-self.packet.get('timestamp_monotonic_s', 0) > .5:
                        raise ValueError('Camera frame is stale; wait for a fresh frame')
                    result = self.calibration.capture(self.raw_frame, self.packet)
                self.calibrating = True
                if self.pipeline:
                    self.pipeline.logic.invalidate('calibration_in_progress')
                return result
            if action == 'preview':
                if self.pipeline is None:
                    raise ValueError('Pipeline not ready')
                return self.calibration.preview(body.get('session_id'), body.get('points_raw'),
                                                body.get('board_id'), self.pipeline.layout, body.get('regions_raw'),
                                                self.pipeline.logic.performance)
            if action == 'save':
                board, path = self.calibration.save(body.get('session_id'), body.get('preview_token'))
                preset_path = calibrated_preset_path(board, path)
                if preset_path is not None:
                    preset = InstrumentPreset.load(preset_path)
                    layout = InstrumentLayout(preset=preset)
                    config = self.interaction_config(preset)
                    self.pipeline.layout = self.pipeline.logic.layout = layout
                    self.pipeline.logic.config = config
                    self.layout_error = None
                    if hasattr(self.pipeline.hands, 'layout'):
                        self.pipeline.hands.layout = layout
                self.pipeline.board = ArucoBoardTracker(board=board, config=self.pipeline.board.config)
                self.pipeline.logic.invalidate('board_changed')
                self.calibrating = False
                return dict(saved=str(path.relative_to(ROOT)), board_id=board.board_id, active=True,
                            preset_saved=None if preset_path is None else str(preset_path.relative_to(ROOT)))
            if action == 'cancel':
                self.calibration.cancel(body.get('session_id'))
                self.calibrating = False
                return dict(cancelled=True)
            raise ValueError('Unknown calibration action')


def handler_for(service):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, status, content, content_type, headers=None):
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(content)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            for name, value in (headers or {}).items():
                self.send_header(name, str(value))
            self.end_headers()
            self.wfile.write(content)

        def send_performance(self, body=None):
            result = service.performance_request(body)
            if result is None:
                self.send(409, b'Performance service not ready', 'text/plain')
            else:
                self.send(200, json.dumps(result, allow_nan=False).encode(), 'application/json')

        def do_GET(self):
            path = urlparse(self.path).path
            try:
                if path in ('/', '/app.js', '/style.css', '/calibration.js'):
                    filename = {'/': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css', '/calibration.js': 'calibration.js'}[path]
                    content_type = {'/': 'text/html; charset=utf-8', '/app.js': 'text/javascript; charset=utf-8',
                                    '/style.css': 'text/css; charset=utf-8', '/calibration.js': 'text/javascript; charset=utf-8'}[path]
                    self.send(200, (ROOT/'web'/filename).read_bytes(), content_type)
                elif path in ('/api/landmarks', '/api/state'):
                    self.send(200, json.dumps(service.latest(), allow_nan=False).encode(), 'application/json')
                elif path in ('/api/v3/landmarks', '/api/v3/state'):
                    self.send(200, json.dumps(service.latest(schema_version=3), allow_nan=False).encode(), 'application/json')
                elif path == '/api/snapshot':
                    packet, jpeg = service.snapshot()
                    body = {'packet': packet, 'image_base64': base64.b64encode(jpeg).decode() if jpeg else None}
                    self.send(200, json.dumps(body, allow_nan=False).encode(), 'application/json')
                elif path == '/api/performance':
                    self.send_performance()
                elif path == '/frame.jpg':
                    packet, jpeg = service.snapshot()
                    self.send(200 if jpeg else 503, jpeg or b'Camera not ready',
                              'image/jpeg' if jpeg else 'text/plain', {'X-Frame-Id': packet.get('frame_id', 0)})
                elif path == '/favicon.ico':
                    self.send(204, b'', 'image/x-icon')
                else:
                    self.send(404, b'Not found', 'text/plain')
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def do_POST(self):
            origin = self.headers.get('Origin')
            if ((origin and origin != f'http://{self.headers.get("Host")}') or
                    self.headers.get('Sec-Fetch-Site') == 'cross-site'):
                self.send(403, b'Cross-origin request rejected', 'text/plain')
                return
            if not ipaddress.ip_address(self.client_address[0]).is_loopback and not service.args.allow_remote_controls:
                self.send(403, b'Remote controls disabled; use SSH forwarding or --allow-remote-controls on a trusted LAN', 'text/plain')
                return
            path = urlparse(self.path).path
            try:
                if path == '/api/performance':
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 0 < length <= 1024:
                        raise ValueError('Performance request must be 1..1024 bytes')
                    body = json.loads(self.rfile.read(length))
                    if not isinstance(body, dict):
                        raise ValueError('Expected a JSON object')
                    self.send_performance(body)
                    return
                elif path.startswith('/api/calibration/'):
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 0 <= length <= 16384:
                        raise ValueError('Request body exceeds 16 KiB')
                    body = json.loads(self.rfile.read(length)) if length else {}
                    if not isinstance(body, dict):
                        raise ValueError('Expected a JSON object')
                    result = service.calibration_request(path.rsplit('/', 1)[-1], body)
                    self.send(200, json.dumps(result, allow_nan=False).encode(), 'application/json')
                    return
                elif path == '/api/record/start':
                    if service.latest()['status'] != 'running':
                        self.send(409, b'Camera not running', 'text/plain')
                        return
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 0 <= length <= 1024:
                        raise ValueError('Recording request too large')
                    body = json.loads(self.rfile.read(length)) if length else {}
                    if not isinstance(body, dict):
                        raise ValueError('Expected a JSON object')
                    raw_frames = body.get('raw_frames', service.args.record_video)
                    if not isinstance(raw_frames, bool):
                        raise ValueError('raw_frames must be true or false')
                    service.set_recording(True, raw_frames)
                elif path == '/api/record/stop':
                    service.set_recording(False)
                elif path == '/api/shutdown':
                    service.stop.set()
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                else:
                    self.send(404, b'Not found', 'text/plain')
                    return
                self.send(200, json.dumps(service.latest(), allow_nan=False).encode(), 'application/json')
            except (ValueError, TypeError, KeyError) as exc:
                self.send(400, str(exc).encode(), 'text/plain; charset=utf-8')
            except (OSError, RuntimeError) as exc:
                self.send(500, str(exc).encode(), 'text/plain; charset=utf-8')
    return Handler


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--camera', type=int, help='Camera index; default prefers Logitech C270')
    parser.add_argument('--list-cameras', action='store_true')
    parser.add_argument('--backend', choices=['auto', 'dshow', 'msmf', 'v4l2', 'gstreamer'], default='auto')
    parser.add_argument('--gstreamer', help='Explicit GStreamer pipeline ending in appsink')
    source = parser.add_mutually_exclusive_group()
    source.add_argument('--demo', action='store_true', help='Scripted hands + generated ArUco, clearly labeled simulation')
    source.add_argument('--video', help='Process a local video through the real ONNX detector')
    parser.add_argument('--width', type=int, default=640)
    parser.add_argument('--height', type=int, default=480)
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--host', default='127.0.0.1', help='Explicit bind address; remote video has no authentication')
    parser.add_argument('--allow-remote-controls', action='store_true', help='Enable record/shutdown for trusted LAN clients')
    parser.add_argument('--no-mirror', action='store_true')
    parser.add_argument('--layout', choices=['standard', 'flip-u'], default='standard')
    parser.add_argument('--interaction-config', help='JSON overrides for InteractionConfig thresholds')
    parser.add_argument('--board', help='Physical BoardDefinition JSON; omitted = automatic legacy corner learning')
    parser.add_argument('--preset', help='InstrumentPreset JSON; default config/presets/guitar.json')
    parser.add_argument('--board-hold', type=float, help='Seconds to hold last reliable pose for display; default .25')
    parser.add_argument('--tracking-config', help='JSON overrides for TrackingConfig quality thresholds')
    parser.add_argument('--audio', choices=['sounddevice', 'mock', 'off'], help='Default sounddevice; demo defaults to mock')
    parser.add_argument('--audio-device', help='Output device index or PortAudio device name')
    parser.add_argument('--record-video', action='store_true', help='Default recording mode includes lossless raw PNG frames')
    parser.add_argument('--duration', type=float, help='Optional bounded processing duration in seconds')
    parser.add_argument('--palm-threshold', type=float, default=.6)
    parser.add_argument('--hand-threshold', type=float, default=.8)
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    if args.list_cameras:
        print(json.dumps(cameras(), ensure_ascii=False, indent=2))
        return
    if (args.width < 1 or args.height < 1 or not 0 < args.palm_threshold <= 1 or
            not 0 < args.hand_threshold <= 1 or (args.duration is not None and args.duration <= 0)):
        parser.error('Dimensions/duration must be positive; thresholds must be in (0, 1].')
    if args.backend == 'gstreamer' and not args.gstreamer:
        parser.error('--backend gstreamer requires --gstreamer')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    service = Service(args)
    server = ThreadingHTTPServer((args.host, args.port), handler_for(service))
    service.worker.start()
    if args.host not in ('127.0.0.1', 'localhost'):
        LOG.warning('Remote viewing enabled without authentication. Use a trusted LAN or SSH tunnel, not the public Internet.')
    LOG.info('Dashboard: http://%s:%s', args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        service.stop.set()
        service.worker.join(timeout=10)
        service.set_recording(False)
        server.server_close()


if __name__ == '__main__':
    main()
