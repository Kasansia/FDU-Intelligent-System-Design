"""Major-scale spelling and immutable performance settings; no device I/O."""
from dataclasses import dataclass

TONICS = ('C', 'Db', 'D', 'Eb', 'E', 'F', 'F#', 'G', 'Ab', 'A', 'Bb', 'B')
INTERVALS = (0, 2, 4, 5, 7, 9, 11)
QUALITIES = ('', 'm', 'm', '', '', 'm', 'dim')
LEGACY_DEGREES = {'C': 1, 'G': 5, 'Am': 6, 'F': 4}
LEGACY_NOTES = {'C': (48, 55, 60, 64, 67), 'G': (43, 50, 55, 59, 62),
                'Am': (45, 52, 57, 60, 64), 'F': (41, 48, 53, 57, 60)}
NATURAL = dict(zip('CDEFGAB', (0, 2, 4, 5, 7, 9, 11)))


@dataclass(frozen=True)
class Performance:
    mode: str = 'chord'
    tonic: str = 'C'
    revision: int = 0

    def __post_init__(self):
        if self.mode not in ('chord', 'melody') or self.tonic not in TONICS:
            raise ValueError('mode must be chord/melody and tonic must be one of '+', '.join(TONICS))
        if type(self.revision) is not int or self.revision < 0:
            raise ValueError('Invalid performance revision')

    def voice(self, degree):
        if type(degree) is not int or degree not in range(1, 8):
            raise ValueError('degree must be an integer in 1..7')
        index = degree-1
        pitch = TONICS.index(self.tonic) + INTERVALS[index]
        letters = 'CDEFGAB'
        letter = letters[(letters.index(self.tonic[0])+index) % 7]
        accidental = (pitch-NATURAL[letter]+6) % 12-6
        name = letter + ('#'*accidental if accidental > 0 else 'b'*(-accidental))
        if self.mode == 'melody':
            midi = 60+pitch
            # Spelled octave differs from sounding octave for B#/Cb.
            octave = (midi-NATURAL[letter]-accidental)//12-1
            return dict(degree=degree, label=f'{name}{octave}', chord=None, notes_midi=[midi])
        root = 48+pitch
        quality = QUALITIES[index]
        notes = [root, root+(4 if quality == '' else 3), root+(6 if quality == 'dim' else 7)]
        return dict(degree=degree, label=name+quality, chord=name+quality, notes_midi=notes)

    def configuration(self):
        return dict(mode=self.mode, tonic=self.tonic, revision=self.revision)

    def snapshot(self):
        return dict(**self.configuration(), tonics=list(TONICS),
                    voices=[self.voice(degree) for degree in range(1, 8)])
