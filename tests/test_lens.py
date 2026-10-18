import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import serial_proto_lens as lens  # noqa: E402

SAMPLE = ROOT / "examples" / "sample_capture.hex"


class TestHexParsing(unittest.TestCase):
    def test_space_separated(self):
        self.assertEqual(lens.parse_hex_text("AA 55 01"), b"\xAA\x55\x01")

    def test_comma_separated(self):
        self.assertEqual(lens.parse_hex_text("AA,55,01"), b"\xAA\x55\x01")

    def test_0x_prefix(self):
        self.assertEqual(lens.parse_hex_text("0xAA 0x55"), b"\xAA\x55")

    def test_long_runs(self):
        self.assertEqual(lens.parse_hex_text("AA5501"), b"\xAA\x55\x01")

    def test_comments_are_stripped(self):
        text = "AA 55  # header\n01 // command\n"
        self.assertEqual(lens.parse_hex_text(text), b"\xAA\x55\x01")

    def test_garbage_is_ignored(self):
        self.assertEqual(lens.parse_hex_text("AA zz 55"), b"\xAA\x55")

    def test_odd_length_token_is_ignored(self):
        self.assertEqual(lens.parse_hex_text("AAA 55"), b"\x55")


class TestCrcVectors(unittest.TestCase):
    """Standard check values for the string '123456789'."""

    CHECK = b"123456789"

    def test_crc8_poly07(self):
        self.assertEqual(lens.crc8(self.CHECK), 0xF4)

    def test_crc16_modbus(self):
        self.assertEqual(lens.crc16_modbus(self.CHECK), 0x4B37)

    def test_crc16_ccitt_false(self):
        self.assertEqual(lens.crc16_ccitt_false(self.CHECK), 0x29B1)

    def test_sum8_wraps(self):
        self.assertEqual(lens.sum8(b"\xFF\x02"), 0x01)

    def test_xor8(self):
        self.assertEqual(lens.xor8(b"\xAA\x55"), 0xFF)


class TestFrameDetection(unittest.TestCase):
    def setUp(self):
        self.data = lens.load_capture(SAMPLE)

    def test_sample_loaded(self):
        self.assertEqual(len(self.data), 120 * 9)

    def test_best_candidate_is_9_bytes(self):
        candidates = lens.find_frame_candidates(self.data)
        self.assertTrue(candidates)
        self.assertEqual(candidates[0].length, 9)
        self.assertEqual(candidates[0].header, b"\xAA\x55")

    def test_candidate_ratio_is_certain(self):
        best = lens.find_frame_candidates(self.data)[0]
        self.assertAlmostEqual(best.ratio, 1.0, places=6)

    def test_split_returns_every_frame(self):
        best = lens.find_frame_candidates(self.data)[0]
        frames = lens.split_frames(self.data, best)
        self.assertEqual(len(frames), 120)
        self.assertTrue(all(len(f) == 9 for f in frames))
        self.assertTrue(all(f.startswith(b"\xAA\x55") for f in frames))

    def test_no_candidate_on_pure_noise(self):
        noise = bytes(range(256)) * 2
        self.assertEqual(lens.find_frame_candidates(noise, min_frames=8), [])

    def test_harmonic_periods_are_dropped(self):
        # The command byte cycles every 5 frames, so a 45-byte period also looks
        # periodic. It is a harmonic of 9 and must not be reported as a framing.
        candidates = lens.find_frame_candidates(self.data)
        lengths = [c.length for c in candidates]
        self.assertEqual(lengths[0], 9)
        self.assertNotIn(45, lengths)
        for length in lengths[1:]:
            self.assertNotEqual(length % 9, 0)


class TestFieldAnalysis(unittest.TestCase):
    def setUp(self):
        data = lens.load_capture(SAMPLE)
        best = lens.find_frame_candidates(data)[0]
        self.frames = lens.split_frames(data, best)
        self.columns = lens.analyse_columns(self.frames)

    def test_header_bytes_are_constant(self):
        self.assertTrue(self.columns[0].constant)
        self.assertTrue(self.columns[1].constant)
        self.assertEqual(self.columns[0].top[0], 0xAA)
        self.assertEqual(self.columns[1].top[0], 0x55)

    def test_length_byte_is_constant(self):
        self.assertTrue(self.columns[2].constant)
        self.assertEqual(self.columns[2].top[0], 4)

    def test_command_byte_varies(self):
        self.assertFalse(self.columns[3].constant)
        self.assertEqual(self.columns[3].distinct, 5)

    def test_sequence_low_byte_looks_like_a_counter(self):
        self.assertEqual(self.columns[6].kind(), "counter")
        self.assertEqual(self.columns[6].step(), 1)

    def test_grouping_merges_the_header(self):
        groups = lens.group_columns(self.columns)
        self.assertEqual(groups[0].start, 0)
        # Bytes 0-2 are all constant (AA, 55 and the fixed length 0x04), so they
        # form a single leading constant field.
        self.assertEqual(groups[0].end, 2)
        self.assertEqual(groups[0].kind, "constant")
        self.assertEqual(groups[0].span(), "bytes 0-2")


class TestChecksumDetection(unittest.TestCase):
    def test_sum8_is_found(self):
        data = lens.load_capture(SAMPLE)
        frames = lens.split_frames(data, lens.find_frame_candidates(data)[0])
        hits = lens.scan_checksums(frames)
        self.assertTrue(hits)
        self.assertEqual(hits[0].algorithm, "sum8")
        self.assertAlmostEqual(hits[0].ratio, 1.0, places=6)
        self.assertEqual(hits[0].coverage, "skipping first 2 bytes")

    def test_wrong_length_finds_nothing(self):
        data = lens.load_capture(SAMPLE)
        bogus = lens.FrameCandidate(b"\xAA\x55", 7, 1.0, 0, 0.0)
        frames = lens.split_frames(data, bogus)
        hits = lens.scan_checksums(frames, min_hit=0.95)
        self.assertEqual(hits, [])


class TestReport(unittest.TestCase):
    def test_report_mentions_key_findings(self):
        data = lens.load_capture(SAMPLE)
        text = lens.render_report(SAMPLE, data, 64)
        self.assertIn("frame length  9 B", text)
        self.assertIn("AA 55", text)
        self.assertIn("sum8", text)


if __name__ == "__main__":
    unittest.main()
