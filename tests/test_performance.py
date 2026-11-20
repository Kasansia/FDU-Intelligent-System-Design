"""Seven-degree mappings, configuration transitions, immutable sound events and compatibility."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import numpy as np
from audio_engine import AudioEngine, MockAudioBackend, SynthSoundBank
from instrument_geometry import InstrumentLayout, project
from instrument_logic import InstrumentLogic, InteractionConfig
from instrument_preset import InstrumentPreset, frame_transform
from music import Performance, TONICS, INTERVALS, LEGACY_NOTES
from pipeline import PerceptionPipeline
from recording import Recorder
from replay import replay_recording, json_lines
from synthetic_input import SyntheticHandTracker, board_image
from tests.helpers import packet
from tests import test_integration


def legacy_preset():
    rect = lambda a, b: [[a, .2], [b, .2], [b, .8], [a, .8]]
    return InstrumentPreset('old', {n: rect(.04+i*.14, .04+(i+1)*.14) for i, n in enumerate(LEGACY_NOTES)},
                            [[.68, .15], [.9, .15], [.9, .85], [.68, .85]], [[.68, .5], [.9, .5]])


def sound_event(performance, degree, event_id=1):
    return dict(**performance.voice(degree), mode=performance.mode, tonic=performance.tonic,
                performance_revision=performance.revision, duration_ms=900,
                event_id=event_id, source_frame_id=1, timestamp_monotonic_s=1., direction='down', velocity=90)


class MusicTests(unittest.TestCase):
    def test_all_keys_degrees_modes_and_spelling(self):
        spellings = ('C D E F G A B', 'Db Eb F Gb Ab Bb C', 'D E F# G A B C#',
                     'Eb F G Ab Bb C D', 'E F# G# A B C# D#', 'F G A Bb C D E',
                     'F# G# A# B C# D# E#', 'G A B C D E F#', 'Ab Bb C Db Eb F G',
                     'A B C# D E F# G#', 'Bb C D Eb F G A', 'B C# D# E F# G# A#')
        suffixes = ('', 'm', 'm', '', '', 'm', 'dim')
        for pc, tonic in enumerate(TONICS):
            for degree, name in enumerate(spellings[pc].split(), 1):
                chord = Performance('chord', tonic).voice(degree)
                melody = Performance('melody', tonic).voice(degree)
                root = 48+pc+INTERVALS[degree-1]
                quality = suffixes[degree-1]
                self.assertEqual(chord['label'], name+quality)
                self.assertEqual(chord['notes_midi'], [root, root+(4 if quality == '' else 3), root+(6 if quality == 'dim' else 7)])
                self.assertEqual(melody['notes_midi'], [root+12])
                self.assertIsNone(melody['chord'])
                self.assertTrue(melody['label'].startswith(name))
        self.assertEqual(Performance('melody', 'D').voice(7)['label'], 'C#5')
        self.assertEqual(Performance('melody', 'F#').voice(7)['label'], 'E#5')
        for args in [('minor', 'C'), ('chord', 'H'), ('melody', 'C', -1)]:
            with self.assertRaises(ValueError):
                Performance(*args)

    def test_every_voice_has_finite_nonzero_audio_and_direction(self):
        bank = SynthSoundBank(8000)
        for mode in ('chord', 'melody'):
            for tonic in TONICS:
                for degree in range(1, 8):
                    e = sound_event(Performance(mode, tonic), degree)
                    down = bank.get_sound(e)
                    up = bank.get_sound(dict(e, direction='up'))
                    self.assertEqual(len(down), 7200)
                    self.assertTrue(np.isfinite(down).all())
                    self.assertGreater(np.max(np.abs(down)), .01)
                    self.assertEqual(np.array_equal(up, down), mode == 'melody')


class TransitionTests(unittest.TestCase):
    def test_polyphony_limit_and_invalid_note_payloads(self):
        backend = MockAudioBackend()
        engine = AudioEngine(backend, sample_rate=8000, max_voices=2, clock=lambda: 1.)
        self.addCleanup(engine.close)
        for i in range(1, 5):
            self.assertTrue(engine.submit(sound_event(Performance(), i, i)))
        output = backend.pump()
        self.assertEqual(len(engine.voices), 2)
        self.assertLessEqual(np.max(np.abs(output)), 1.)
        for notes in ([], [True], [float('nan')], [128], [[60]], [60]*6):
            self.assertFalse(engine.submit(dict(sound_event(Performance(), 1, 5), notes_midi=notes)))

    def test_switch_requires_new_dwell_and_crossing_without_reusing_event_id(self):
        now = [0.]
        logic = InstrumentLogic(clock=lambda: now[0])
        def step(t, v=.3):
            now[0] = t
            return logic.process(packet(t, left=(.08, .5), right=(.78, v)))
        step(0); step(.11)
        first = step(.2, .65)[1][0]
        logic.set_performance(Performance('melody', 'D', 1))
        self.assertIsNone(logic._state()['selected_degree'])
        self.assertFalse(step(.22, .3)[0]['selection_valid'])
        self.assertEqual(step(.33, .3)[1], [])
        state, events = step(.42, .65)
        self.assertTrue(state['selection_valid'])
        self.assertFalse(state['chord_valid'])
        self.assertEqual(state['selected_label'], 'D4')
        self.assertEqual(events[0]['notes_midi'], [62])
        self.assertIsNone(events[0]['chord'])
        self.assertEqual(events[0]['event_id'], first['event_id']+1)
        self.assertEqual(events[0]['performance_revision'], 1)

    def test_queued_old_events_dropped_and_active_voice_fades(self):
        backend = MockAudioBackend()
        engine = AudioEngine(backend, sample_rate=8000, clock=lambda: 1.)
        self.addCleanup(engine.close)
        self.assertTrue(engine.submit(sound_event(Performance(), 1)))
        backend.pump(40)
        self.assertTrue(engine.submit(sound_event(Performance(), 2, 2)))
        engine.set_performance_revision(1)
        self.assertFalse(engine.submit(sound_event(Performance(), 3, 3)))
        fading = backend.pump(80)
        self.assertGreater(np.max(np.abs(fading)), 0)
        self.assertTrue(np.all(backend.pump(80) == 0))
        self.assertEqual([e['event_id'] for e in backend.events], [1])
        event = sound_event(Performance('melody', 'G', 1), 7, 4)
        self.assertTrue(engine.submit(event))
        event['notes_midi'][0] = 0  # The queued event must own its payload.
        backend.pump()
        self.assertEqual(backend.events[-1]['notes_midi'], [78])
        self.assertFalse(engine.submit(sound_event(Performance('melody', 'G', 1), 7, 4)))

    def test_settings_change_while_callback_dequeues(self):
        backend = MockAudioBackend()
        engine = AudioEngine(backend, sample_rate=8000, clock=lambda: 1.)
        self.addCleanup(engine.close)
        self.assertTrue(engine.submit(sound_event(Performance(), 1)))
        get_nowait = engine.queue.get_nowait

        def switch_before_dequeue():
            dequeue.side_effect = get_nowait
            engine.set_performance_revision(1)
            self.assertTrue(engine.submit(sound_event(Performance('melody', 'D', 1), 2, 2)))
            return get_nowait()

        with patch.object(engine.queue, 'get_nowait', side_effect=switch_before_dequeue) as dequeue:
            backend.pump()
        self.assertEqual([event['event_id'] for event in backend.events], [2])
        self.assertEqual(engine.snapshot()['dropped_events'], 1)

    def test_settings_change_frees_full_queue_before_next_callback(self):
        backend = MockAudioBackend()
        engine = AudioEngine(backend, sample_rate=8000, queue_size=1, clock=lambda: 1.)
        self.addCleanup(engine.close)
        self.assertTrue(engine.submit(sound_event(Performance(), 1)))
        engine.set_performance_revision(0)
        self.assertEqual(engine.snapshot()['queue_depth'], 1)

        current = Performance('melody', 'D', 1)
        engine.set_performance_revision(current.revision)
        self.assertTrue(engine.submit(sound_event(current, 2, 2)))
        backend.pump()
        self.assertEqual([event['event_id'] for event in backend.events], [2])
        self.assertEqual(engine.snapshot()['dropped_events'], 1)

    def test_melody_seven_cells_both_mirrors_and_missing_hands(self):
        for flip in (False, True):
            layout = InstrumentLayout(flip_u=flip)
            for degree in range(1, 8):
                now = [0.]
                logic = InstrumentLogic(layout, clock=lambda: now[0], performance=Performance('melody', 'G'))
                for t in (0, .11):
                    now[0] = t
                    state, events = logic.process(packet(t, left=layout.local((.04+(degree-.5)*.08, .5)), right=None))
                    self.assertEqual(events, [])
                self.assertTrue(state['selection_valid'])
                self.assertEqual(state['selected_degree'], degree)
                now[0] = .2
                state, events = logic.process(packet(.2, left=None, right=layout.local((.78, .7))))
                self.assertFalse(state['selection_valid'])
                self.assertEqual(events, [])


class CompatibilityTests(unittest.TestCase):
    def test_rapid_recording_restart_with_same_wall_clock(self):
        from datetime import datetime
        with tempfile.TemporaryDirectory() as root, patch('recording.datetime') as date:
            date.now.return_value = datetime(2026, 11, 6, 12, 0, 0)
            first = Recorder(root)
            first.close()
            second = Recorder(root)
            second.close()
            self.assertNotEqual(first.folder, second.folder)

    def test_existing_111_and_default_upgrade_without_writes(self):
        old = legacy_preset()
        self.assertEqual(old.for_live().schema_version, 2)
        path = Path(__file__).resolve().parents[1]/'config/presets/111.json'
        # The actual user configuration is optional in other checkouts.
        configs = [old]
        original = path.read_bytes() if path.exists() else None
        if original:
            configs.append(InstrumentPreset.load(path))
        for config in configs:
            before = config.to_dict()
            upgraded = config.for_live()
            self.assertEqual(len(upgraded.degree_regions), 7)
            if config.chord_frame is not None:
                np.testing.assert_array_equal(upgraded.selection_frame, config.chord_frame)
            inverse = np.linalg.inv(frame_transform(upgraded.selection_frame))
            layout = InstrumentLayout(preset=upgraded)
            for i in range(7):
                point = project([[(i+.5)/7, .5]], inverse)[0]
                self.assertEqual(layout.chord_at(point), str(i+1))
            self.assertEqual(config.to_dict(), before)
        if original:
            self.assertEqual(path.read_bytes(), original)
        unusual = InstrumentPreset('custom', {'C': [[0, 0], [1, 0], [0, 1]]}, old.strum, old.axis)
        with self.assertRaisesRegex(ValueError, '重新划定'):
            unusual.for_live()

    def test_recorded_mode_changes_replay_without_recreating_tracker(self):
        from aruco_tracking import ArucoBoardTracker
        with tempfile.TemporaryDirectory() as root:
            now = [1.]
            pipeline = PerceptionPipeline(hands=SyntheticHandTracker(), logic=InstrumentLogic(clock=lambda: now[0]))
            recorder = Recorder(root, raw_frames=True)
            expected = []
            for i in range(48):
                if i in (16, 32):
                    pipeline.logic.set_performance(Performance('melody' if i == 16 else 'chord', 'D' if i == 16 else 'G', i//16))
                now[0] = 1+i*.08
                raw = board_image()
                _, p = pipeline.process(raw, 1000+i*.08, now[0])
                self.assertTrue(recorder.submit(p, raw, pipeline.configuration()))
                expected.extend((e['event_id'], e['mode'], e['tonic'], e['notes_midi'], e['direction']) for e in p['events'])
            recorder.close()
            self.assertIsNone(recorder.error)
            target = Path(root)/'replay.jsonl'
            with patch('replay.ArucoBoardTracker', wraps=ArucoBoardTracker) as tracker:
                replay_recording(recorder.folder, target)
            self.assertEqual(tracker.call_count, 1)
            actual = [(e['event_id'], e['mode'], e['tonic'], e['notes_midi'], e['direction']) for p in json_lines(target) for e in p['events']]
            self.assertEqual(actual, expected)
            self.assertEqual({e[1] for e in actual}, {'chord', 'melody'})

    def test_legacy_recording_keeps_four_regions_and_original_voicings(self):
        with tempfile.TemporaryDirectory() as root:
            now = [1.]
            layout = InstrumentLayout(preset=legacy_preset())
            pipeline = PerceptionPipeline(hands=SyntheticHandTracker(layout=layout), layout=layout,
                                          logic=InstrumentLogic(layout, InteractionConfig(strum_path_enabled=False,
                                                                                         selection_margin_fraction=None),
                                                                clock=lambda: now[0]))
            recorder = Recorder(root, raw_frames=True)
            expected = []
            for i in range(18):
                now[0] = 1+i*.08
                raw = board_image()
                _, p = pipeline.process(raw, 1000+i*.08, now[0])
                config = pipeline.configuration()
                config.pop('performance')  # Historical recordings have no performance settings.
                for key in ('strum_path_enabled', 'strum_tracking_margin', 'selection_margin_fraction'):
                    config['interaction'].pop(key)
                recorder.submit(p, raw, config)
                expected.extend((e['event_id'], e['direction'], e['chord'], e['notes_midi']) for e in p['events'])
            recorder.close()
            target = Path(root)/'old.jsonl'
            replay_recording(recorder.folder, target)
            packets = list(json_lines(target))
            self.assertEqual(len(packets[0]['layout']['chords']), 4)
            events = [e for p in packets for e in p['events']]
            self.assertTrue(events)
            self.assertEqual([(e['event_id'], e['direction'], e['chord'], e['notes_midi']) for e in events], expected)
            for event in events:
                self.assertEqual(event['notes_midi'], list(LEGACY_NOTES[event['chord']]))

    def test_layout_requiring_calibration_stays_silent_in_replay(self):
        old = legacy_preset()
        preset = InstrumentPreset('custom', {'C': old.chords['C']}, old.strum, old.axis)
        layout = InstrumentLayout(preset=preset)
        with tempfile.TemporaryDirectory() as root:
            now = [1.]
            pipeline = PerceptionPipeline(hands=SyntheticHandTracker(layout=layout), layout=layout,
                                          logic=InstrumentLogic(layout, clock=lambda: now[0]))
            recorder = Recorder(root, raw_frames=True)
            raw = board_image()
            for i in range(18):
                now[0] = 1+i*.08
                _, frame = pipeline.process(raw, 1000+i*.08, now[0], interaction_enabled=False)
                frame['interaction'] = pipeline.logic.invalidate('layout_requires_calibration')
                self.assertTrue(recorder.submit(frame, raw, pipeline.configuration()))
            recorder.close()
            target = Path(root)/'replay.jsonl'
            result = replay_recording(recorder.folder, target)
            self.assertEqual(result['frames'], 18)
            self.assertEqual(result['generated_events'], 0)
            self.assertEqual(result['audio']['played_events'], 0)
            self.assertEqual({frame['interaction']['reason'] for frame in json_lines(target)},
                             {'layout_requires_calibration'})


class PerformanceHTTPTests(unittest.TestCase):
    setUp = test_integration.HTTPTests.setUp
    tearDown = test_integration.HTTPTests.tearDown

    def request(self, body=None, headers=None):
        data = None if body is None else json.dumps(body).encode()
        with urlopen(Request(self.url+'/api/performance', data=data, headers=headers or {}), timeout=3) as response:
            return json.load(response)

    def test_controls_validation_persistence_and_calibration_interlock(self):
        self.service.worker.start()
        deadline = time.monotonic()+5
        while self.service.latest()['status'] == 'starting' and time.monotonic() < deadline:
            self.service.stop.wait(.01)
        initial = self.request()
        self.assertEqual(initial['mode'], 'chord')
        self.assertTrue(initial['service_instance_id'])
        changed = self.request({'mode': 'melody', 'tonic': 'D'})
        self.assertEqual(changed['revision'], 1)
        self.assertEqual(changed['service_instance_id'], initial['service_instance_id'])
        self.assertEqual(self.request(), changed)
        self.assertEqual(self.request({'mode': 'melody', 'tonic': 'D'})['revision'], 1)
        for bad in ({'mode': 'minor', 'tonic': 'C'}, {'mode': 'chord', 'tonic': 'H'}, {'tonic': 'C'}, []):
            with self.assertRaises(HTTPError) as error:
                self.request(bad)
            self.assertEqual(error.exception.code, 400)
        with self.assertRaises(HTTPError) as error:
            urlopen(Request(self.url+'/api/performance', data=b'null'), timeout=3)
        self.assertEqual(error.exception.code, 400)
        with self.assertRaises(HTTPError) as error:
            self.request({'mode': 'chord', 'tonic': 'C'}, {'Origin': 'https://evil.invalid'})
        self.assertEqual(error.exception.code, 403)
        with patch.object(self.service.calibration, 'active', return_value=True):
            with self.assertRaises(HTTPError):
                self.request({'mode': 'chord', 'tonic': 'C'})
        self.assertEqual(self.request()['revision'], 1)
        for _ in range(100):
            state = self.service.latest(schema_version=3)
            if state.get('performance', {}).get('revision') == 1:
                break
            self.service.stop.wait(.01)
        self.assertEqual(state['performance']['tonic'], 'D')
        self.assertEqual(len(state['layout']['degree_regions']), 7)
        self.assertEqual(state['service_instance_id'], initial['service_instance_id'])
        self.assertEqual(self.service.snapshot()[0]['service_instance_id'], initial['service_instance_id'])

    def test_controls_reject_requests_after_worker_ends_on_its_own(self):
        self.service.args.duration = .1
        self.service.worker.start()
        self.service.worker.join(timeout=5)
        self.assertFalse(self.service.worker.is_alive())
        self.assertFalse(self.service.stop.is_set())
        self.assertEqual(self.service.latest()['status'], 'stopped')
        for body in (None, {'mode': 'melody', 'tonic': 'D'}):
            with self.assertRaises(HTTPError) as error:
                self.request(body)
            self.assertEqual(error.exception.code, 409)
