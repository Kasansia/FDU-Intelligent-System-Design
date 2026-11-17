import json
import unittest
import numpy as np
from audio_engine import AudioEngine, MockAudioBackend


def event(i=1, chord='C', direction='down', velocity=90, t=10.):
    return {'event_id': i, 'source_frame_id': i, 'timestamp_monotonic_s': t,
            'chord': chord, 'direction': direction, 'velocity': velocity}


class AudioTests(unittest.TestCase):
    def make(self, **kwargs):
        backend = MockAudioBackend()
        engine = AudioEngine(backend, clock=lambda: 10., **kwargs)
        self.addCleanup(engine.close)
        return engine, backend

    def test_exactly_once_with_metadata_and_overlap(self):
        engine, backend = self.make()
        for i, chord in enumerate(('C', 'G', 'Am', 'F'), 1):
            e = event(i, chord, 'up' if i % 2 else 'down')
            self.assertTrue(engine.submit(e))
            self.assertFalse(engine.submit(e))
        block = backend.pump()
        self.assertEqual([e['chord'] for e in backend.events], ['C', 'G', 'Am', 'F'])
        self.assertEqual([e['velocity'] for e in backend.events], [90]*4)
        self.assertEqual(len(engine.voices), 4)
        self.assertGreater(np.max(np.abs(block)), 0)
        backend.pump()
        self.assertEqual(len(backend.events), 4)
        self.assertEqual(engine.snapshot()['duplicate_events'], 4)

    def test_amplitude_and_direction(self):
        low, lb = self.make()
        high, hb = self.make()
        low.submit(event(velocity=20))
        high.submit(event(velocity=100))
        np.testing.assert_allclose(hb.pump(), lb.pump()*5, atol=1e-7)
        self.assertFalse(np.allclose(high.sound_bank.get_sound(event(direction='up')), high.sound_bank.get_sound(event(direction='down'))))

    def test_bounded_queue_and_stale_events(self):
        engine, backend = self.make(queue_size=1)
        self.assertTrue(engine.submit(event()))
        self.assertFalse(engine.submit(event(2)))
        self.assertEqual(engine.snapshot()['queue_depth'], 1)
        backend.pump()
        self.assertTrue(engine.submit(event(3, t=9)))
        backend.pump()
        self.assertEqual(len(backend.events), 1)
        self.assertEqual(engine.snapshot()['dropped_events'], 2)
        json.dumps(engine.snapshot(), allow_nan=False)

    def test_unavailable_device_is_reported(self):
        class Broken(MockAudioBackend):
            def start(self, *args):
                raise RuntimeError('No output device')
        engine = AudioEngine(Broken())
        self.addCleanup(engine.close)
        self.assertEqual(engine.snapshot()['status'], 'error')
        self.assertFalse(engine.submit(event()))
        self.assertIn('No output device', engine.snapshot()['error'])

    def test_invalid_event_and_disabled(self):
        engine, backend = self.make(enabled=False)
        self.assertFalse(engine.submit(event()))
        self.assertEqual(engine.snapshot()['status'], 'disabled')
        engine, backend = self.make()
        for bad in (event(chord='Dm'), event(velocity=0), event(direction='side'), event(t=float('nan'))):
            self.assertFalse(engine.submit(bad))
