import { test } from 'node:test';
import assert from 'node:assert/strict';
import { MathUtils, Vector3 } from 'three';
import { boundedTilt, pressedTilt } from '../src/relief-angle.ts';

test('rotation reaches 40 degrees while diagonals stay inside the same cone', () => {
  for (const [x, y] of [[0, 0], [40, 0], [0, -40], [40, 40], [-200, 40], [2, -7]]) {
    const [a, b] = boundedTilt(x, y);
    const normal = new Vector3(0, 0, 1).applyQuaternion(pressedTilt(x, y));
    const angle = MathUtils.radToDeg(normal.angleTo(new Vector3(0, 0, 1)));
    assert.ok(Math.hypot(a, b) <= 40 + 1e-10);
    assert.ok(Math.abs(angle - Math.min(40, Math.hypot(x, y))) < 1e-9);
    if (Math.hypot(x, y) <= 40) assert.deepEqual([a, b], [x, y]);
  }
});

test('hovered edges and corners press away from the camera, including top and bottom', () => {
  for (const [x, y] of [[-40, 0], [40, 0], [0, -40], [0, 40], [-40, -40], [40, -40], [-40, 40], [40, 40]]) {
    const rotation = pressedTilt(x, y);
    const pressedEdge = new Vector3(Math.sign(x), -Math.sign(y), 0);
    const oppositeEdge = pressedEdge.clone().negate();
    assert.ok(pressedEdge.applyQuaternion(rotation).z < 0, `hover ${x},${y} must press the indicated edge inward`);
    assert.ok(oppositeEdge.applyQuaternion(rotation).z > 0);
  }
});
