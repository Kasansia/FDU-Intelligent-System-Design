"""Local webcam worker and loopback-only dashboard / JSON API."""
import argparse
import csv
import json
import logging
from pathlib import Path
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
import cv2 as cv
from cv2_enumerate_cameras import enumerate_cameras
from hand_tracking import ROOT, HandTracker, annotate

LOG = logging.getLogger('hand-tracking')


def cameras(backend=cv.CAP_DSHOW):
    return [{'index': c.index, 'name': c.name, 'backend': c.backend}
            for c in enumerate_cameras(backend)]


class Service:
    def __init__(self, args):
        self.args = args
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.packet = {'hands': [], 'status': 'starting'}
        self.jpeg = None
        self.backend_name = None
        self.recording = None
        self.last_recording = None
        self.device_list = cameras()
        selected = next((c for c in self.device_list if 'c270' in c['name'].lower()), None)
        selected = selected or next((c for c in self.device_list if 'ir camera' not in c['name'].lower()), None)
        self.index = args.camera if args.camera is not None else (selected['index'] if selected else 0)
        self.camera_name = next((c['name'] for c in self.device_list if c['index'] == self.index), str(self.index))
        self.worker = threading.Thread(target=self.run, daemon=True)

    def set_recording(self, enabled):
        with self.lock:
            if enabled and self.recording is None:
                folder = ROOT / 'outputs' / time.strftime('%Y%m%d_%H%M%S')
                folder.mkdir(parents=True, exist_ok=True)
                json_file = (folder / 'landmarks.jsonl').open('a', encoding='utf-8')
                csv_file = (folder / 'landmarks.csv').open('a', newline='', encoding='utf-8')
                writer = csv.writer(csv_file)
                writer.writerow(['frame_id', 'timestamp_unix_s', 'track_id', 'handedness', 'confidence',
                                 'node_id', 'node_name', 'x_px', 'y_px', 'z_relative_px',
                                 'x_normalized', 'y_normalized', 'z_normalized', 'world_x_m', 'world_y_m', 'world_z_m'])
                self.recording = (json_file, csv_file, writer, str(folder))
                self.last_recording = str(folder)
            elif not enabled and self.recording:
                self.recording[0].close()
                self.recording[1].close()
                self.recording = None

    def latest(self):
        with self.lock:
            return dict(self.packet, recording=self.recording is not None,
                        last_recording=self.last_recording, camera_name=self.camera_name,
                        camera_index=self.index, camera_backend=self.backend_name, available_cameras=self.device_list)

    def run(self):
        cap = None
        try:
            cv.setNumThreads(4)
            tracker = HandTracker(self.args.palm_threshold, self.args.hand_threshold, mirror=not self.args.no_mirror)
            backends = {'auto': [cv.CAP_DSHOW, cv.CAP_MSMF], 'dshow': [cv.CAP_DSHOW], 'msmf': [cv.CAP_MSMF]}
            frame = None
            for backend in backends[self.args.backend]:
                backend_devices = cameras(backend)
                device = next((c for c in backend_devices if c['name'] == self.camera_name), None)
                index = device['index'] if device else self.index
                LOG.info('Opening %s via %s', self.camera_name, backend)
                cap = cv.VideoCapture(index, backend)
                if cap.isOpened():
                    ok, frame = cap.read()
                    if ok and frame is not None:
                        self.backend_name = cap.getBackendName()
                        break
                cap.release()
                cap = None
            if cap is None:
                raise RuntimeError(f'Cannot open camera {self.index}: {self.camera_name}; close other camera apps and check USB connection.')
            cap.set(cv.CAP_PROP_FRAME_WIDTH, self.args.width)
            cap.set(cv.CAP_PROP_FRAME_HEIGHT, self.args.height)
            cap.set(cv.CAP_PROP_FPS, 30)
            LOG.info('Camera opened: %s (index %s, %s)', self.camera_name, self.index, self.backend_name)
            previous, fps, failed = time.perf_counter(), 0.0, 0
            while not self.stop.is_set():
                ok, frame = cap.read()
                captured = time.time()
                if not ok or frame is None:
                    failed += 1
                    with self.lock:
                        self.packet = dict(self.packet, status='error', error='Camera frame unavailable', hands=[])
                        self.jpeg = None
                    if failed >= 30:
                        raise RuntimeError('Camera disconnected or frames unavailable; restart after reconnecting.')
                    self.stop.wait(.1)
                    continue
                failed = 0
                display, packet = tracker.process(frame, captured)
                now = time.perf_counter()
                instant = 1 / max(now - previous, .0001)
                fps = instant if not fps else .9 * fps + .1 * instant
                previous = now
                packet.update(status='running', fps=round(fps, 1))
                ok, encoded = cv.imencode('.jpg', annotate(display, packet), [cv.IMWRITE_JPEG_QUALITY, 85])
                if not ok:
                    raise RuntimeError('JPEG encoding failed')
                with self.lock:
                    self.packet, self.jpeg = packet, encoded.tobytes()
                    if self.recording:
                        jf, cf, writer, _ = self.recording
                        jf.write(json.dumps(packet, separators=(',', ':'), allow_nan=False) + '\n')
                        for hand in packet['hands']:
                            for i, name in enumerate(packet['landmark_names']):
                                writer.writerow([packet['frame_id'], captured, hand['track_id'], hand['handedness'],
                                                 hand['confidence'], i, name, *hand['landmarks_px'][i],
                                                 *hand['landmarks_normalized'][i], *hand['landmarks_world_m'][i]])
                        jf.flush()
                        cf.flush()
        except Exception as exc:
            LOG.exception('Camera worker failed')
            with self.lock:
                self.packet = dict(self.packet, status='error', error=str(exc), hands=[])
                self.jpeg = None
        finally:
            if cap is not None:
                cap.release()
            self.set_recording(False)


def handler_for(service):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, status, content, content_type):
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(content)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(content)

        def do_GET(self):
            path = urlparse(self.path).path
            try:
                if path == '/':
                    self.send(200, (ROOT / 'web/index.html').read_bytes(), 'text/html; charset=utf-8')
                elif path == '/api/landmarks':
                    self.send(200, json.dumps(service.latest(), allow_nan=False).encode(), 'application/json')
                elif path == '/frame.jpg':
                    with service.lock:
                        jpeg = service.jpeg
                    self.send(200 if jpeg else 503, jpeg or b'Camera not ready', 'image/jpeg' if jpeg else 'text/plain')
                else:
                    self.send(404, b'Not found', 'text/plain')
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def do_POST(self):
            # No CORS; also block cross-origin form submissions to local controls.
            origin = self.headers.get('Origin')
            if origin and origin != f'http://{self.headers.get("Host")}':
                self.send(403, b'Cross-origin request rejected', 'text/plain')
                return
            path = urlparse(self.path).path
            if path == '/api/record/start':
                if service.latest()['status'] != 'running':
                    self.send(409, b'Camera not running', 'text/plain')
                    return
                service.set_recording(True)
            elif path == '/api/record/stop':
                service.set_recording(False)
            elif path == '/api/shutdown':
                service.stop.set()
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self.send(404, b'Not found', 'text/plain')
                return
            self.send(200, json.dumps(service.latest()).encode(), 'application/json')
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--camera', type=int, help='DirectShow index; default prefers Logitech C270')
    parser.add_argument('--list-cameras', action='store_true')
    parser.add_argument('--backend', choices=['auto', 'dshow', 'msmf'], default='auto')
    parser.add_argument('--width', type=int, default=640)
    parser.add_argument('--height', type=int, default=480)
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--no-mirror', action='store_true')
    parser.add_argument('--palm-threshold', type=float, default=.6)
    parser.add_argument('--hand-threshold', type=float, default=.8)
    args = parser.parse_args()
    if args.list_cameras:
        print(json.dumps(cameras(), ensure_ascii=False, indent=2))
        return
    if args.width < 1 or args.height < 1 or not 0 < args.palm_threshold <= 1 or not 0 < args.hand_threshold <= 1:
        parser.error('Dimensions must be positive; thresholds must be in (0, 1].')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    service = Service(args)
    server = ThreadingHTTPServer(('127.0.0.1', args.port), handler_for(service))
    service.worker.start()
    LOG.info('Dashboard: http://127.0.0.1:%s', args.port)
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
