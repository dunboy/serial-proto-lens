#!/usr/bin/env python3
"""serial-proto-lens — infer the frame structure of an undocumented serial protocol.

You captured bytes off a UART / RS-485 / BLE-serial link and the vendor manual is
missing, wrong, or incomplete. This tool answers three questions from the capture
alone:

  1. Where does a frame start, and how long is it?
  2. Which byte offsets are constant (header, address) and which vary (payload)?
  3. Is there a checksum at the end, and which algorithm produced it?

No third-party dependencies. Python 3.9+.

Typical use:

    python serial_proto_lens.py report capture.hex
    python serial_proto_lens.py frames capture.hex --length 9
    python serial_proto_lens.py checksum capture.hex --length 9
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

HEX_SPLIT = re.compile(r"[\s,;|:]+")
HEX_DIGITS = set("0123456789abcdefABCDEF")


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------


def parse_hex_text(text: str) -> bytes:
    """Parse a hex dump into raw bytes.

    Accepts ``AA 55 01``, ``AA,55,01``, ``0xAA 0x55`` and long runs like ``AA5501``.
    Everything after ``#`` or ``//`` on a line is treated as a comment, so annotated
    captures (``AA 55  # header``) work too.
    """
    out = bytearray()
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].split("//", 1)[0]
        for token in HEX_SPLIT.split(line):
            token = token.strip()
            if not token:
                continue
            if token[:2].lower() == "0x":
                token = token[2:]
            if not token or len(token) % 2 or any(c not in HEX_DIGITS for c in token):
                continue
            out.extend(bytes.fromhex(token))
    return bytes(out)


def load_capture(path: Path) -> bytes:
    """Read a capture from disk. Text-looking files are parsed as hex dumps."""
    raw = path.read_bytes()
    if path.suffix.lower() in {".hex", ".txt", ".log", ".csv"}:
        parsed = parse_hex_text(raw.decode("utf-8", errors="replace"))
        if parsed:
            return parsed
    return raw


# --------------------------------------------------------------------------
# frame boundary detection
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FrameCandidate:
    header: bytes
    length: int
    ratio: float
    frames: int
    score: float

    def header_hex(self) -> str:
        return " ".join(f"{b:02X}" for b in self.header)

    def describe(self) -> str:
        return (
            f"header {self.header_hex():<6} frame length {self.length:>2} B "
            f"\u00b7 {self.ratio * 100:5.1f}% of gaps \u00b7 ~{self.frames} frames"
        )


def find_frame_candidates(
    data: bytes,
    min_len: int = 3,
    max_len: int = 64,
    min_frames: int = 4,
    min_ratio: float = 0.6,
) -> list[FrameCandidate]:
    """Guess frame length by looking for a header that repeats at a fixed interval.

    For every 1- and 2-byte sequence we record where it occurs, then ask: do the
    distances between occurrences cluster on a single value? A real protocol header
    repeats at exactly one interval, so one gap dominates. Payload bytes do not.
    """
    n = len(data)
    if n < min_len * min_frames:
        return []

    occurrences: dict[bytes, list[int]] = {}
    for unit_len in (2, 1):
        for i in range(n - unit_len + 1):
            occurrences.setdefault(data[i : i + unit_len], []).append(i)

    found: dict[int, FrameCandidate] = {}
    for header, positions in occurrences.items():
        if len(positions) < min_frames:
            continue
        gaps = [b - a for a, b in zip(positions, positions[1:])]
        if not gaps:
            continue
        gap, count = Counter(gaps).most_common(1)[0]
        ratio = count / len(gaps)
        if ratio < min_ratio or not (min_len <= gap <= max_len):
            continue
        frames = count + 1
        if frames < min_frames:
            continue
        # Prefer longer headers (more specific) and then steadier intervals.
        score = len(header) + ratio + min(frames, 100) / 100.0
        current = found.get(gap)
        if current is None or score > current.score:
            found[gap] = FrameCandidate(header, gap, ratio, frames, score)

    ordered = sorted(found.values(), key=lambda c: (c.length, -c.score))
    kept: list[FrameCandidate] = []
    for cand in ordered:
        # Drop harmonics: a 45-byte period on a 9-byte protocol is just three
        # frames, not a competing framing hypothesis.
        if any(cand.length % k.length == 0 and k.length < cand.length for k in kept):
            continue
        kept.append(cand)

    return sorted(kept, key=lambda c: (-c.score, c.length))


def split_frames(data: bytes, candidate: FrameCandidate) -> list[bytes]:
    """Cut the stream into frames of `candidate.length`, resyncing on mismatch."""
    frames: list[bytes] = []
    start = data.find(candidate.header)
    while start != -1:
        end = start + candidate.length
        if end > len(data):
            break
        chunk = data[start:end]
        if chunk.startswith(candidate.header):
            frames.append(chunk)
            start = end
        else:
            start = data.find(candidate.header, start + 1)
    return frames


# --------------------------------------------------------------------------
# per-offset field analysis
# --------------------------------------------------------------------------


@dataclass
class Column:
    offset: int
    values: list[int]

    @property
    def distinct(self) -> int:
        return len(set(self.values))

    @property
    def top(self) -> tuple[int, int]:
        return Counter(self.values).most_common(1)[0]

    @property
    def mode_ratio(self) -> float:
        return self.top[1] / len(self.values)

    @property
    def constant(self) -> bool:
        return self.distinct == 1

    def step(self) -> int | None:
        """Return the constant additive step across frames, if this looks like a counter."""
        if len(self.values) < 4 or self.distinct < 2:
            return None
        steps = [(b - a) % 256 for a, b in zip(self.values, self.values[1:])]
        step, count = Counter(steps).most_common(1)[0]
        if step != 0 and count / len(steps) >= 0.9:
            return step
        return None

    def kind(self) -> str:
        if self.constant:
            return "constant"
        if self.step() is not None:
            return "counter"
        return "varying"


def analyse_columns(frames: list[bytes]) -> list[Column]:
    if not frames:
        return []
    width = len(frames[0])
    return [Column(i, [f[i] for f in frames]) for i in range(width)]


@dataclass
class FieldGroup:
    start: int
    end: int
    kind: str

    def span(self) -> str:
        if self.start == self.end:
            return f"byte {self.start}"
        return f"bytes {self.start}-{self.end}"


def group_columns(columns: list[Column]) -> list[FieldGroup]:
    """Merge neighbouring offsets that behave the same way into one field."""
    groups: list[FieldGroup] = []
    for col in columns:
        kind = col.kind()
        if groups and groups[-1].kind == kind and groups[-1].end == col.offset - 1:
            groups[-1].end = col.offset
        else:
            groups.append(FieldGroup(col.offset, col.offset, kind))
    return groups


# --------------------------------------------------------------------------
# checksum detection
# --------------------------------------------------------------------------


def sum8(buf: bytes) -> int:
    return sum(buf) & 0xFF


def xor8(buf: bytes) -> int:
    acc = 0
    for b in buf:
        acc ^= b
    return acc


def crc8(buf: bytes, poly: int = 0x07, init: int = 0x00) -> int:
    crc = init
    for b in buf:
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ poly) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def crc16_modbus(buf: bytes, poly: int = 0xA001, init: int = 0xFFFF) -> int:
    crc = init
    for b in buf:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ poly if crc & 1 else crc >> 1
    return crc & 0xFFFF


def crc16_ccitt_false(buf: bytes, poly: int = 0x1021, init: int = 0xFFFF) -> int:
    crc = init
    for b in buf:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ poly) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


ALGORITHMS: dict[str, tuple[int, object]] = {
    "sum8": (1, sum8),
    "xor8": (1, xor8),
    "crc8/0x07": (1, crc8),
    "crc16/modbus": (2, crc16_modbus),
    "crc16/ccitt-false": (2, crc16_ccitt_false),
}

COVERAGES: dict[str, object] = {
    "whole frame minus checksum": lambda f, w: f[:-w],
    "skipping first byte": lambda f, w: f[1:-w],
    "skipping first 2 bytes": lambda f, w: f[2:-w],
}


@dataclass
class ChecksumHit:
    algorithm: str
    coverage: str
    endian: str
    ratio: float


def scan_checksums(frames: list[bytes], min_hit: float = 0.9) -> list[ChecksumHit]:
    """Test trailing-checksum hypotheses against every frame."""
    if len(frames) < 3:
        return []
    hits: list[ChecksumHit] = []
    for name, (width, fn) in ALGORITHMS.items():
        endians = ("little", "big") if width == 2 else ("n/a",)
        for cov_name, coverage in COVERAGES.items():
            for endian in endians:
                ok = 0
                usable = 0
                for frame in frames:
                    if len(frame) <= width + 2:
                        continue
                    usable += 1
                    payload = coverage(frame, width)
                    expected = fn(payload)
                    if width == 2:
                        actual = int.from_bytes(frame[-2:], endian)
                    else:
                        actual = frame[-1]
                    if actual == expected:
                        ok += 1
                if usable and ok / usable >= min_hit:
                    hits.append(ChecksumHit(name, cov_name, endian, ok / usable))
    return sorted(hits, key=lambda h: (-h.ratio, h.algorithm))


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------


def _hex(values: list[int], limit: int = 6) -> str:
    seen: list[str] = []
    for v in values:
        s = f"{v:02X}"
        if s not in seen:
            seen.append(s)
        if len(seen) >= limit:
            break
    suffix = ", ..." if len(set(values)) > len(seen) else ""
    return " ".join(seen) + suffix


def render_report(path: Path, data: bytes, max_len: int) -> str:
    lines: list[str] = []
    lines.append("serial-proto-lens")
    lines.append(f"file   {path}")
    lines.append(f"bytes  {len(data)}")
    lines.append("")

    candidates = find_frame_candidates(data, max_len=max_len)
    if not candidates:
        lines.append("No repeating frame boundary found.")
        lines.append("Try a larger --max-len, or feed more capture data.")
        return "\n".join(lines)

    lines.append("[1] frame boundaries")
    for i, cand in enumerate(candidates[:3]):
        mark = "*" if i == 0 else " "
        lines.append(f"  {mark} {cand.describe()}")
    if len(candidates) > 3:
        lines.append(f"    (+{len(candidates) - 3} more candidates)")
    lines.append("")

    best = candidates[0]
    frames = split_frames(data, best)
    lines.append(f"[2] field map \u00b7 {len(frames)} frames \u00d7 {best.length} B")
    columns = analyse_columns(frames)
    lines.append("  off  values              distinct  note")
    for col in columns:
        note = col.kind()
        if col.constant:
            note = f"constant {col.top[0]:02X}"
        elif note == "counter":
            note = f"counter, step {col.step():+d}"
        lines.append(
            f"  {col.offset:>3}  {_hex(col.values):<19} {col.distinct:>8}  {note}"
        )
    lines.append("")

    groups = group_columns(columns)
    lines.append("  grouped fields")
    for group in groups:
        label = group.kind
        if group.kind == "constant":
            values = [columns[i].top[0] for i in range(group.start, group.end + 1)]
            label = "constant " + " ".join(f"{v:02X}" for v in values)
        elif group.kind == "counter":
            label = f"counter, step {columns[group.start].step():+d}"
        lines.append(f"    {group.span():<12} {label}")
    lines.append("")

    lines.append("[3] checksum (trailing field)")
    hits = scan_checksums(frames)
    if hits:
        for hit in hits[:5]:
            endian = "" if hit.endian == "n/a" else f", {hit.endian}-endian"
            lines.append(
                f"  * {hit.algorithm:<18} over {hit.coverage:<28} "
                f"{hit.ratio * 100:5.1f}% match{endian}"
            )
    else:
        lines.append("  No common checksum matched \u226590% of frames.")
        lines.append("  It may be a custom algorithm, or the field is not trailing.")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="serial-proto-lens",
        description="Infer frame structure, field roles and checksum algorithm "
        "from a raw serial capture.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_report = sub.add_parser("report", help="full analysis in one shot")
    p_report.add_argument("capture", type=Path)
    p_report.add_argument("--max-len", type=int, default=64, help="longest frame to consider")

    p_frames = sub.add_parser("frames", help="dump frames as hex")
    p_frames.add_argument("capture", type=Path)
    p_frames.add_argument("--length", type=int, required=True)
    p_frames.add_argument("--header", default="", help="header bytes, e.g. AA55")
    p_frames.add_argument("--limit", type=int, default=20)

    p_check = sub.add_parser("checksum", help="only run checksum detection")
    p_check.add_argument("capture", type=Path)
    p_check.add_argument("--length", type=int, required=True)
    p_check.add_argument("--header", default="")

    return parser


def _frames_for(args) -> list[bytes]:
    data = load_capture(args.capture)
    if args.header:
        header = bytes.fromhex(args.header.replace(" ", ""))
        candidate = FrameCandidate(header, args.length, 1.0, 0, 0.0)
    else:
        candidates = find_frame_candidates(data, max_len=max(args.length, 64))
        if not candidates:
            raise SystemExit("No frame boundary found; pass --header explicitly.")
        candidate = candidates[0]
        candidate = FrameCandidate(
            candidate.header, args.length, candidate.ratio, candidate.frames, candidate.score
        )
    return split_frames(data, candidate)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "report":
        data = load_capture(args.capture)
        print(render_report(args.capture, data, args.max_len))
        return 0

    frames = _frames_for(args)

    if args.command == "frames":
        for frame in frames[: args.limit]:
            print(" ".join(f"{b:02X}" for b in frame))
        print(f"# {len(frames)} frames, {args.length} B each")
        return 0

    hits = scan_checksums(frames)
    if not hits:
        print("No common checksum matched \u226590% of frames.")
        return 1
    for hit in hits[:5]:
        endian = "" if hit.endian == "n/a" else f", {hit.endian}-endian"
        print(
            f"{hit.algorithm:<18} over {hit.coverage:<28} "
            f"{hit.ratio * 100:5.1f}% match{endian}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
