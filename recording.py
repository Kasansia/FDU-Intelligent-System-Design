"""Bounded asynchronous packet recording, separate from the audio callback."""
import csv
from datetime import datetime
import json
import queue
from pathlib import Path
import threading
import tempfile
from copy import deepcopy
import hashlib
import os
import cv2 as cv

COLUMNS = ['frame_id', 'timestamp_unix_s', 'track_id', 'handedness', 'confidence',
           'node_id', 'node_name', 'x_px', 'y_px', 'z_relative_px',
           'x_normalized', 'y_normalized', 'z_normalized', 'world_x_m', 'world_y_m', 'world_z_m',
           'instrument_u', 'instrument_v']


class Recorder:
    def __init__(self, root, capacity=128, raw_frames=False, metadata=None):
        Path(root).mkdir(parents=True, exist_ok=True)
        self.folder = Path(tempfile.mkdtemp(prefix=datetime.now().strftime('%Y%m%d_%H%M%S_%f')+'_', dir=root))
        if capacity < 1:
            raise ValueError('Recording capacity must be positive')
        self.raw_frames = raw_frames
        self.metadata = metadata or {}
        self.queue = queue.Queue(maxsize=min(capacity, 16) if raw_frames else capacity)
        self.stop = threading.Event()
        self.error = None
        self.frames = 0
        self.raw_saved = self.accepted = 0
        self.dropped = []
        self.last_submitted_id = None
        if raw_frames:
            (self.folder/'frames').mkdir()
        self._manifest('recording')
        self.thread = threading.Thread(target=self._run, name='packet-recorder', daemon=True)
        self.thread.start()

    def submit(self, packet, raw_frame=None, configuration=None):
        if self.stop.is_set() or self.error:
            return False
        frame_id = packet.get('frame_id')
        if (not isinstance(frame_id, int) or frame_id < 0 or
                (self.last_submitted_id is not None and frame_id <= self.last_submitted_id)):
            self.error = 'Recording requires strictly increasing integer frame_id'
            self.dropped.append(dict(frame_id=frame_id, reason=self.error))
            self.stop.set()
            return False
        self.last_submitted_id = frame_id
        # Copy before enqueue: camera buffers and caller dictionaries may be reused.
        item = (deepcopy(packet), raw_frame.copy() if self.raw_frames and raw_frame is not None else None,
                deepcopy(configuration))
        try:
            self.queue.put_nowait(item)
            self.accepted += 1
            return True
        except queue.Full:
            self.error = 'Recording queue full; recording stopped. Some frames were not saved.'
            self.dropped.append(dict(frame_id=frame_id, reason='queue_full'))
            self.stop.set()
            return False

    def snapshot(self):
        return dict(raw_frames=self.raw_frames, accepted_frames=self.accepted, written_frames=self.frames,
                    raw_saved_frames=self.raw_saved, dropped_frames=list(self.dropped), error=self.error,
                    queue_depth=self.queue.qsize())

    def _manifest(self, status):
        data = dict(recording_schema_version=1, status=status, raw_format='lossless_png' if self.raw_frames else None,
                    metadata=self.metadata, **self.snapshot())
        data['complete'] = status == 'closed' and self.error is None and self.frames == self.accepted and (
            not self.raw_frames or self.raw_saved == self.frames)
        temporary = self.folder/'manifest.tmp'
        temporary.write_text(json.dumps(data, allow_nan=False, indent=2)+'\n', encoding='utf-8')
        os.replace(temporary, self.folder/'manifest.json')

    def _run(self):
        try:
            with ((self.folder/'landmarks.jsonl').open('w', encoding='utf-8') as jf,
                  (self.folder/'landmarks.csv').open('w', newline='', encoding='utf-8') as cf,
                  (self.folder/'events.jsonl').open('w', encoding='utf-8') as ef,
                  (self.folder/'frames.jsonl').open('w', encoding='utf-8') as ff,
                  (self.folder/'configurations.jsonl').open('w', encoding='utf-8') as configs):
                writer = csv.writer(cf)
                writer.writerow(COLUMNS)
                last_event = 0
                known_configs = set()
                while not self.stop.is_set() or not self.queue.empty():
                    try:
                        packet, raw, configuration = self.queue.get(timeout=.05)
                    except queue.Empty:
                        continue
                    if configuration is not None:
                        content = json.dumps(configuration, sort_keys=True, separators=(',', ':'), allow_nan=False)
                        config_id = hashlib.sha256(content.encode()).hexdigest()
                        if config_id not in known_configs:
                            configs.write(json.dumps(dict(config_id=config_id, configuration=configuration), allow_nan=False)+'\n')
                            configs.flush()
                            known_configs.add(config_id)
                        packet['recording_config_id'] = config_id
                    if self.raw_frames:
                        index = dict(frame_id=packet['frame_id'], timestamp_unix_s=packet['timestamp_unix_s'],
                                     timestamp_monotonic_s=packet['timestamp_monotonic_s'], path=None, status='failed')
                        try:
                            if raw is None or raw.shape[:2] != (packet['frame_height'], packet['frame_width']):
                                raise ValueError('Missing raw frame or packet/image dimensions differ')
                            ok, encoded = cv.imencode('.png', raw, [cv.IMWRITE_PNG_COMPRESSION, 1])
                            if not ok:
                                raise RuntimeError('Raw PNG encoding failed')
                            relative = f"frames/{packet['frame_id']:012d}.png"
                            encoded.tofile(self.folder/relative)
                            index.update(path=relative, status='saved', sha256=hashlib.sha256(encoded).hexdigest())
                            self.raw_saved += 1
                        except Exception as exc:
                            index['error'] = str(exc)
                            self.error = 'Raw recording failure: '+str(exc)
                            self.stop.set()
                        packet['raw_frame_recording'] = index
                        ff.write(json.dumps(index, allow_nan=False)+'\n')
                        ff.flush()
                    jf.write(json.dumps(packet, separators=(',', ':'), allow_nan=False)+'\n')
                    for hand in packet['hands']:
                        for i, name in enumerate(packet['landmark_names']):
                            uv = hand.get('landmarks_instrument_uv')
                            writer.writerow([packet['frame_id'], packet['timestamp_unix_s'], hand['track_id'],
                                             hand['handedness'], hand['confidence'], i, name,
                                             *hand['landmarks_px'][i], *hand['landmarks_normalized'][i],
                                             *hand['landmarks_world_m'][i], *(uv[i] if uv is not None else ['', ''])])
                    for event in packet.get('events', []):
                        if event['event_id'] > last_event:
                            ef.write(json.dumps(event, allow_nan=False)+'\n')
                            last_event = event['event_id']
                    self.frames += 1
                    jf.flush()
                    cf.flush()
                    ef.flush()
        except Exception as exc:
            self.error = str(exc)
            self.stop.set()
        finally:
            try:
                self._manifest('closed')
            except OSError as exc:
                self.error = 'Cannot finalize recording manifest: '+str(exc)

    def close(self):
        self.stop.set()
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            self.error = 'Recorder is still draining after 5 seconds; check disk performance.'
