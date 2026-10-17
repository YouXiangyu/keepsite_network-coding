import { MathUtils, Quaternion, Vector3 } from 'three';

export const MAX_RELIEF_TILT = 40;

/** A single axis-angle rotation caps the total deviation, including diagonals. */
export function boundedTilt(horizontal: number, vertical: number): [number, number] {
  const distance = Math.hypot(horizontal, vertical);
  const factor = distance > MAX_RELIEF_TILT ? MAX_RELIEF_TILT / distance : 1;
  return [horizontal * factor, vertical * factor];
}

/** Pointer Y grows downwards; the indicated edge moves away from the +Z camera. */
export function pressedTilt(horizontal: number, vertical: number): Quaternion {
  const [x, y] = boundedTilt(horizontal, vertical);
  const angle = Math.hypot(x, y);
  return angle
    ? new Quaternion().setFromAxisAngle(new Vector3(y, x, 0).normalize(), MathUtils.degToRad(angle))
    : new Quaternion();
}
