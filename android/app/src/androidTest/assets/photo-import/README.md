# Synthetic HEIF import vectors

These fixtures were generated for keepsite tests on 2026-10-25 with Pillow 12.1.0 and pillow-heif 1.8.1. They contain only a 96 by 64 four-quadrant pattern (top-left red, top-right green, bottom-left blue, bottom-right white). No personal photo or third-party image content is included.

| File | Expected displayed dimensions | Top-left / top-right / bottom-left / bottom-right | SHA-256 |
| --- | --- | --- | --- |
| quadrants.heic | 96 x 64 | red / green / blue / white | c1afec8036c3136e750a28c5a53fc14a703cd45ad4ed2945d0e8a8c4228e70a6 |
| rotate90-container.heic | 64 x 96 | blue / red / white / green | 65935f15a8636d36985c228f1d82bff3a82c974d75d9092d6d52e9f011696b9a |
| mirror-container.heic | 96 x 64 | green / red / white / blue | 43f4cfa7ab5122cd7646d14d45477147a4772ab938e2d678efa58dfbf1521f86 |

The latter two vectors contain HEIF `irot` and `imir` container transforms respectively. Expected results were independently decoded with Pillow before the Android test. Conversion preserves the input bytes, applies the orientation once and emits a standalone JPEG. The generator is a test preparation tool, not an Android runtime dependency.
