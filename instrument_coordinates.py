"""Pure 2-D coordinate conversions and additive schema-v2 packet fusion."""
import numpy as np


def flip_frame_xy(points, frame_width, mirrored):
    """Convert raw <-> display XY (the horizontal reflection is its own inverse)."""
    xy = np.asarray(points, dtype=np.float64).copy()
    if xy.ndim != 2 or xy.shape[1] != 2:
        raise ValueError('Expected an N x 2 array')
    if mirrored:
        xy[:, 0] = frame_width - 1 - xy[:, 0]
    return xy


def project_xy(points, homography):
    """Project N x 2 points without clamping. Return None at a projective horizon.

    Unlike perspectiveTransform's zero at a zero denominator, None cannot be
    mistaken for a real point at the instrument origin. Reject the whole array
    if any point cannot be represented as a finite pair.
    """
    xy = np.asarray(points, dtype=np.float64)
    matrix = np.asarray(homography, dtype=np.float64)
    if xy.ndim != 2 or xy.shape[1] != 2 or matrix.shape != (3, 3):
        raise ValueError('Expected N x 2 points and a 3 x 3 homography')
    if not np.isfinite(xy).all() or not np.isfinite(matrix).all():
        return None
    scale = np.max(np.abs(matrix))
    if scale == 0:
        return None
    homogeneous = np.c_[xy, np.ones(len(xy))] @ (matrix / scale).T
    if not np.isfinite(homogeneous).all() or np.any(np.abs(homogeneous[:, 2]) < 1e-10):
        return None
    uv = homogeneous[:, :2] / homogeneous[:, 2:3]
    return uv if np.isfinite(uv).all() else None


def hand_to_instrument_uv(landmarks_px, frame_width, mirrored, homography):
    """Map displayed hand XY back to raw pixels, then to planar instrument UV."""
    if homography is None:
        return None
    points = np.asarray(landmarks_px, dtype=np.float64)
    raw_xy = flip_frame_xy(points[:, :2], frame_width, mirrored)
    uv = project_xy(raw_xy, homography)
    return None if uv is None else uv.tolist()


def fuse_instrument_coordinates(hand_packet, instrument_packet):
    """Return an augmented copy; old hand fields and their semantics are preserved."""
    usable = instrument_packet['status'] in ('valid', 'held')
    homography = instrument_packet['homography_camera_to_instrument'] if usable else None
    hands = []
    for hand in hand_packet['hands']:
        uv = hand_to_instrument_uv(hand['landmarks_px'], hand_packet['frame_width'],
                                   hand_packet['mirrored'], homography)
        hands.append(dict(hand, landmarks_instrument_uv=uv))
    return dict(hand_packet, schema_version=2, hands=hands, instrument=instrument_packet)
