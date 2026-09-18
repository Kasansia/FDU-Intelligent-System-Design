# OpenCV Zoo runtime sources

Upstream: https://github.com/opencv/opencv_zoo

Pinned commit: `47534e27c9851bb1128ccc0102f1145e27f23f98`

This directory vendors only the two Python wrappers required by this project:

- `models/palm_detection_mediapipe/mp_palmdet.py`
- `models/handpose_estimation_mediapipe/mp_handpose.py`

The upstream Apache-2.0 license is preserved at the root and in both model directories.
Corresponding ONNX files are stored in the project's `models/` directory; hashes and
official download URLs are defined in `download_models.py`.

Local modification to both wrappers: replace `cv.dnn.readNet(self.model_path)` with
`cv.dnn.readNetFromONNX(np.fromfile(self.model_path, dtype=np.uint8))` so models load
from Windows paths containing Chinese characters. No inference or postprocessing
math has been changed.

The original full local clone, `vendor/opencv_zoo/`, is excluded from the parent
repository. No submodule initialization is required to use these vendored files.
