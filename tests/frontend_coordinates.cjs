const assert = require('node:assert/strict');
const {calibrationRawPoint, calibrationRectangle} = require('../web/calibration.js');
for (const mirrored of [true, false]) {
  for (const box of [{left:20, top:30, width:1000, height:300}, {left:5, top:8, width:240, height:600}]) {
    const width = 640, height = 480, raw = [123, 234];
    const scale = Math.min(box.width/width, box.height/height);
    const displayX = mirrored ? width-1-raw[0] : raw[0];
    const x = box.left+(box.width-width*scale)/2+displayX*scale;
    const y = box.top+(box.height-height*scale)/2+raw[1]*scale;
    const actual = calibrationRawPoint(x, y, box, width, height, mirrored);
    assert.ok(Math.hypot(actual[0]-raw[0], actual[1]-raw[1]) < 1e-9);
    assert.equal(calibrationRawPoint(box.left, box.top, box, width, height, mirrored), null);
    for (const [start, end] of [[[100, 80], [300, 240]], [[300, 240], [100, 80]], [[100, 240], [300, 80]]]) {
      const fromDisplay = p => [mirrored ? width-1-p[0] : p[0], p[1]];
      const rect = calibrationRectangle(fromDisplay(start), fromDisplay(end), mirrored).map(fromDisplay);
      assert.deepEqual(rect, [[100, 80], [300, 80], [300, 240], [100, 240]]);
    }
  }
}
console.log('PASS: mirror, scale, horizontal and vertical letterboxing use the same raw point');
