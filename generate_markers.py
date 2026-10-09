"""Generate four printable DICT_4X4_50 PNGs with quiet borders, using OpenCV only."""
import argparse
from pathlib import Path
import cv2 as cv


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path(__file__).resolve().parent / 'outputs/markers')
    parser.add_argument('--pixels', type=int, default=600, help='Black marker square side, excluding white border')
    args = parser.parse_args()
    if args.pixels < 60 or args.pixels % 6:
        parser.error('--pixels must be a multiple of 6 and at least 60')
    args.output.mkdir(parents=True, exist_ok=True)
    dictionary = cv.aruco.getPredefinedDictionary(cv.aruco.DICT_4X4_50)
    border = args.pixels // 6
    for marker_id in range(4):
        marker = cv.aruco.generateImageMarker(dictionary, marker_id, args.pixels)
        printable = cv.copyMakeBorder(marker, border, border, border, border, cv.BORDER_CONSTANT, value=255)
        path = args.output / f'DICT_4X4_50_ID_{marker_id}.png'
        success, data = cv.imencode('.png', printable)
        if not success:
            raise RuntimeError('PNG encoding failed')
        data.tofile(path)  # Unicode-safe Windows path.
        print(path)
    print('Print the black square at about 50 mm; keep the surrounding white border.')
    print('Layout by instrument coordinates: 0 1 / 2 3. No printer scaling calibration is performed.')


if __name__ == '__main__':
    main()
