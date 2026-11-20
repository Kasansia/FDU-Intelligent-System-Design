"""Bounded musical-event queue and polyphonic, pre-generated local test tones.

No file loading, subprocesses or blocking playback on the perception thread.
The PortAudio callback consumes each accepted event once, independently of HTTP.
"""
from collections import deque
import math
import queue
import threading
import time
import numpy as np
from music import Performance, TONICS, LEGACY_NOTES
from timing import monotonic_s

NOTES = LEGACY_NOTES


def synthesize_chords(sample_rate=48000, duration=.9):
    """Original additive synthesis; no third-party sound recordings."""
    sounds = {}
    t = np.arange(int(sample_rate * duration), dtype=np.float64) / sample_rate
    envelope = (1 - np.exp(-t * 500)) * np.exp(-t * 5)
    envelope *= np.minimum(1., (duration - t) * 100)
    for chord, notes in NOTES.items():
        for direction in ('up', 'down'):
            data = np.zeros(len(t), np.float32)
            ordered = notes if direction == 'down' else tuple(reversed(notes))
            for i, midi in enumerate(ordered):
                freq = 440 * 2 ** ((midi - 69) / 12)
                tone = (np.sin(2*np.pi*freq*t) + .25*np.sin(4*np.pi*freq*t) + .1*np.sin(6*np.pi*freq*t)) * envelope
                offset = int(sample_rate * .008 * i)
                data[offset:] += (tone[:len(t)-offset] * .08).astype(np.float32)
            sounds[chord, direction] = data
    return sounds


class SynthSoundBank:
    """Pre-rendered event voices. A future sampler can implement the same lookup."""
    def __init__(self, sample_rate):
        self.sounds = {}
        note_sets = set(LEGACY_NOTES.values())
        for mode in ('chord', 'melody'):
            for tonic in TONICS:
                note_sets.update(tuple(Performance(mode, tonic).voice(i)['notes_midi']) for i in range(1, 8))
        t = np.arange(int(sample_rate*.9), dtype=np.float64)/sample_rate
        envelope = (1-np.exp(-t*500))*np.exp(-t*5)*np.minimum(1., (.9-t)*100)
        tones = {}
        for midi in set(n for notes in note_sets for n in notes):
            phase = 2*np.pi*(440*2**((midi-69)/12))*t
            tones[midi] = ((np.sin(phase)+.25*np.sin(2*phase)+.1*np.sin(3*phase))*envelope).astype(np.float32)
        for notes in note_sets:
            for direction in ('down', 'up'):
                sound = np.zeros(len(t), np.float32)
                for i, midi in enumerate(notes if direction == 'down' else reversed(notes)):
                    offset = int(sample_rate*.008*i)
                    sound[offset:] += tones[midi][:len(t)-offset]*(.25/np.sqrt(len(notes)))
                self.sounds[notes, direction] = sound

    def get_sound(self, event):
        notes = event.get('notes_midi', LEGACY_NOTES.get(event.get('chord'), ()))
        return self.sounds.get((tuple(notes), event['direction']))


class MockAudioBackend:
    name = 'mock'
    def __init__(self):
        self.events, self.render = [], None
        self.error = None

    def start(self, sample_rate, render):
        self.render = render

    def begin_event(self, event):
        self.events.append(dict(event))

    def pump(self, frames=512):
        return self.render(frames)

    def close(self):
        pass


class SoundDeviceBackend:
    name = 'sounddevice'
    def __init__(self, device=None):
        self.device, self.stream, self.error = device, None, None
        self.underflows = 0

    def start(self, sample_rate, render):
        import sounddevice as sd

        def callback(outdata, frames, timing, status):
            if status.output_underflow:
                self.underflows += 1
            try:
                outdata[:, 0] = render(frames)
            except Exception as exc:
                self.error = str(exc)
                outdata.fill(0)
                raise sd.CallbackAbort from exc
        self.stream = sd.OutputStream(device=self.device, samplerate=sample_rate, channels=1,
                                      dtype='float32', latency='low', blocksize=0, callback=callback)
        self.stream.start()

    def begin_event(self, event):
        pass

    def close(self):
        if self.stream is not None:
            self.stream.stop()
            self.stream.close()


class AudioEngine:
    def __init__(self, backend=None, sample_rate=48000, queue_size=8, max_voices=8,
                 max_event_age_s=.2, clock=monotonic_s, enabled=True, sound_bank=None):
        if queue_size < 1 or max_voices < 1 or sample_rate < 8000 or max_event_age_s <= 0:
            raise ValueError('Invalid audio configuration')
        self.backend = backend if backend is not None else SoundDeviceBackend()
        self.sample_rate, self.clock = sample_rate, clock
        self.queue = queue.Queue(maxsize=queue_size)
        self.max_voices, self.max_event_age_s = max_voices, max_event_age_s
        self.sound_bank = sound_bank or SynthSoundBank(sample_rate)
        self.voices, self.latencies_ms = [], deque(maxlen=256)
        self.submit_lock = threading.Lock()
        self.performance_revision = self.render_revision = 0
        self.fade_voices = []
        self.fade_remaining = 0
        self.high_water = self.accepted = self.played = self.dropped = self.duplicates = 0
        self.error = None
        self.status = 'starting' if enabled else 'disabled'
        if enabled:
            try:
                self.backend.start(sample_rate, self.render)
                self.status = 'mock' if self.backend.name == 'mock' else 'ready'
            except Exception as exc:
                self.status, self.error = 'error', str(exc)
                try:
                    self.backend.close()
                except Exception:
                    pass

    def submit(self, event):
        """Return immediately. IDs are monotonic; old/replayed IDs never re-enter playback."""
        try:
            valid = (type(event['event_id']) is int and event['event_id'] > 0 and
                     event['direction'] in ('up', 'down') and
                     type(event['velocity']) is int and 1 <= event['velocity'] <= 127 and
                     math.isfinite(event['timestamp_monotonic_s']) and
                     type(event.get('performance_revision', 0)) is int and
                     event.get('duration_ms', 900) == 900)
            if 'notes_midi' in event:
                notes = event['notes_midi']
                valid = valid and isinstance(notes, (list, tuple)) and 1 <= len(notes) <= 5 and all(type(n) is int and 0 <= n <= 127 for n in notes)
            valid = valid and self.sound_bank.get_sound(event) is not None
        except (KeyError, TypeError, ValueError):
            valid = False
        if not valid:
            return False
        with self.submit_lock:
            if event.get('performance_revision', 0) != self.performance_revision:
                self.dropped += 1
                return False
            if event['event_id'] <= self.high_water:
                self.duplicates += 1
                return False

            self.high_water = event['event_id']
            if self.status not in ('ready', 'mock') or self.backend.error:
                self.dropped += 1
                return False
            try:
                queued = dict(event)
                if 'notes_midi' in queued:
                    queued['notes_midi'] = list(queued['notes_midi'])
                self.queue.put_nowait(queued)
                self.accepted += 1
                return True
            except queue.Full:
                self.dropped += 1
                return False

    def set_performance_revision(self, revision):
        """Clear pending old events; leave active voices to the audio callback."""
        with self.submit_lock:
            if revision == self.performance_revision:
                return
            self.performance_revision = revision
            while True:
                try:
                    self.queue.get_nowait()
                except queue.Empty:
                    break
                self.dropped += 1

    def render(self, frames):
        # Sole owner of voices: audio callback (or explicitly driven mock).
        events = []
        for _ in range(self.queue.maxsize):
            try:
                events.append(self.queue.get_nowait())
            except queue.Empty:
                break
        # Read the settings after dequeuing so the batch can include concurrent
        # submissions from a new revision. Later changes apply to the next buffer.
        now = self.clock()
        revision = self.performance_revision
        if revision != self.render_revision:
            self.fade_voices = self.voices
            self.voices = []
            self.fade_remaining = max(1, int(self.sample_rate*.01))
            self.render_revision = revision
        for event in events:
            age = now - event['timestamp_monotonic_s']
            if event.get('performance_revision', 0) != revision or age < -.01 or age > self.max_event_age_s:
                self.dropped += 1
                continue
            if len(self.voices) >= self.max_voices:
                self.voices.pop(0)
            self.voices.append([self.sound_bank.get_sound(event), 0, event['velocity']/127])
            self.backend.begin_event(event)
            self.played += 1
            self.latencies_ms.append(max(0, age)*1000)
        output = np.zeros(frames, np.float32)
        if self.fade_remaining:
            fade_count = min(frames, self.fade_remaining)
            fade = (self.fade_remaining-np.arange(fade_count))/max(1, int(self.sample_rate*.01))
            for voice in self.fade_voices:
                sound, offset, gain = voice
                count = min(fade_count, len(sound)-offset)
                output[:count] += sound[offset:offset+count]*gain*fade[:count]
                voice[1] += count
            self.fade_remaining -= fade_count
            if not self.fade_remaining:
                self.fade_voices = []
        remaining = []
        for sound, offset, gain in self.voices:
            count = min(frames, len(sound)-offset)
            output[:count] += sound[offset:offset+count] * gain
            if offset + count < len(sound):
                remaining.append([sound, offset+count, gain])
        self.voices = remaining
        return np.clip(output, -1, 1)

    def snapshot(self):
        error = self.error or self.backend.error
        stream = getattr(self.backend, 'stream', None)
        if self.status == 'ready' and stream is not None and not stream.active:
            error = error or 'Audio output stream is inactive'
        values = list(self.latencies_ms)
        return {'status': 'error' if error else self.status, 'backend': self.backend.name, 'error': error,
                'queue_depth': self.queue.qsize(), 'queue_capacity': self.queue.maxsize,
                'accepted_events': self.accepted, 'played_events': self.played,
                'dropped_events': self.dropped, 'duplicate_events': self.duplicates,
                'output_underflows': getattr(self.backend, 'underflows', 0),
                'event_to_callback_ms_p50': float(np.median(values)) if values else None,
                'event_to_callback_ms_p95': float(np.percentile(values, 95)) if values else None}

    def close(self):
        try:
            self.backend.close()
        finally:
            self.status = 'closed'


def main():
    import argparse
    import json
    parser = argparse.ArgumentParser(description='Play seven scale degrees without camera or UI')
    parser.add_argument('--list-devices', action='store_true')
    parser.add_argument('--device', help='PortAudio output device name or index')
    parser.add_argument('--mode', choices=['chord', 'melody'], default='chord')
    parser.add_argument('--tonic', choices=TONICS, default='C')
    args = parser.parse_args()
    if args.list_devices:
        import sounddevice as sd
        print(sd.query_devices())
        return
    device = int(args.device) if args.device and args.device.isdigit() else args.device
    engine = AudioEngine(SoundDeviceBackend(device))
    try:
        if engine.snapshot()['status'] == 'error':
            raise RuntimeError(engine.snapshot()['error'])
        performance = Performance(args.mode, args.tonic)
        for i in range(1, 8):
            engine.submit({'event_id': i, 'source_frame_id': 0, 'timestamp_monotonic_s': monotonic_s(),
                           **performance.voice(i), 'mode': performance.mode, 'tonic': performance.tonic,
                           'performance_revision': 0, 'duration_ms': 900, 'direction': 'down', 'velocity': 90})
            time.sleep(.4)
        time.sleep(1.)
        print(json.dumps(engine.snapshot(), indent=2))
    finally:
        engine.close()


if __name__ == '__main__':
    main()
