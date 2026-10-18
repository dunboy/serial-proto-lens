#!/usr/bin/env python3
"""Generate a synthetic capture so the tool has something realistic to chew on.

The pretend device speaks a 9-byte protocol:

    AA 55 | LEN | CMD | D0 D1 D2 D3 | SUM8

where LEN is the payload length, CMD cycles through five commands, the payload
carries a slowly rising 16-bit reading plus a 16-bit sequence number, and SUM8 is
the low byte of the sum of everything from LEN to the last payload byte.

Run it to regenerate ``sample_capture.hex``:

    python examples/make_sample.py
"""

from __future__ import annotations

from pathlib import Path

HEADER = b"\xAA\x55"
FRAME_COUNT = 120


def build_frame(cmd: int, payload: bytes) -> bytes:
    body = bytes([len(payload), cmd]) + payload
    checksum = sum(body) & 0xFF
    return HEADER + body + bytes([checksum])


def main() -> None:
    frames = []
    for seq in range(FRAME_COUNT):
        cmd = 0x01 + (seq % 5)
        reading = 200 + seq * 3
        payload = bytes(
            [
                reading & 0xFF,
                (reading >> 8) & 0xFF,
                seq & 0xFF,
                (seq >> 8) & 0xFF,
            ]
        )
        frames.append(build_frame(cmd, payload))

    lines = [
        "# Synthetic capture for serial-proto-lens.",
        "# Layout: AA 55 | LEN | CMD | D0 D1 D2 D3 | SUM8   (9 bytes per frame)",
        "",
    ]
    lines += [" ".join(f"{b:02X}" for b in frame) for frame in frames]

    dest = Path(__file__).with_name("sample_capture.hex")
    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    total = sum(len(f) for f in frames)
    print(f"wrote {len(frames)} frames / {total} bytes -> {dest}")


if __name__ == "__main__":
    main()
