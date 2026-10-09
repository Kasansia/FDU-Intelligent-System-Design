"""Read the running camera's landmarks without opening the camera a second time."""
import json
import time
import urllib.request


def main():
    previous = None
    while True:
        with urllib.request.urlopen('http://127.0.0.1:8765/api/landmarks', timeout=2) as response:
            packet = json.load(response)
        if packet.get('status') != 'running' or time.time() - packet.get('timestamp_unix_s', 0) > .5:
            print('No fresh camera data')
        elif packet['frame_id'] != previous:
            previous = packet['frame_id']
            for hand in packet['hands']:
                fingertip = hand['landmarks_normalized'][8]
                print(packet['frame_id'], hand['handedness'], hand['track_id'], 'index tip:', fingertip)
                uv = hand.get('landmarks_instrument_uv')
                instrument = packet.get('instrument', {})
                if uv is not None and instrument.get('status') in ('valid', 'held'):
                    print('  instrument index tip (u, v):', uv[8],
                          'status:', instrument['status'], 'H age:', instrument.get('homography_age_s'))
                else:
                    print('  instrument unavailable')
        time.sleep(.02)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
