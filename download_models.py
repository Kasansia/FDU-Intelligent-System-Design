"""Fetch official pinned Zoo models and verify Git LFS SHA-256 values."""
import hashlib
from pathlib import Path
import urllib.request

COMMIT = '47534e27c9851bb1128ccc0102f1145e27f23f98'
MODELS = {
    'palm_detection_mediapipe': '78ff51c38496b7fc8b8ebdb6cc8c1abb02fa6c38427c6848254cdaba57fcce7c',
    'handpose_estimation_mediapipe': 'db0898ae717b76b075d9bf563af315b29562e11f8df5027a1ef07b02bef6d81c',
}


def main():
    zoo = Path(__file__).resolve().parent / 'vendor/opencv_zoo_runtime/models'
    for relative in ['palm_detection_mediapipe/mp_palmdet.py', 'handpose_estimation_mediapipe/mp_handpose.py']:
        source = zoo / relative
        text = source.read_text(encoding='utf-8')
        original = 'self.model = cv.dnn.readNet(self.model_path)'
        patched = 'self.model = cv.dnn.readNetFromONNX(np.fromfile(self.model_path, dtype=np.uint8))'
        if original in text:
            source.write_text(text.replace(original, patched), encoding='utf-8')
        elif patched not in text:
            raise RuntimeError(f'Unexpected Zoo source version: {source}')
    root = Path(__file__).resolve().parent / 'models'
    root.mkdir(exist_ok=True)
    for model, expected in MODELS.items():
        name = model + '_2023feb.onnx'
        target = root / name
        if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == expected:
            print('Verified:', name)
            continue
        url = f'https://media.githubusercontent.com/media/opencv/opencv_zoo/{COMMIT}/models/{model}/{name}'
        with urllib.request.urlopen(url, timeout=90) as response:
            data = response.read()
        if hashlib.sha256(data).hexdigest() != expected:
            raise RuntimeError(f'Model checksum mismatch: {name}')
        temporary = target.with_suffix('.download')
        temporary.write_bytes(data)
        temporary.replace(target)
        print('Downloaded and verified:', name)


if __name__ == '__main__':
    main()
