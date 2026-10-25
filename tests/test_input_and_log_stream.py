"""Offline PTY input and incrementally written JSONL regression tests.

No CLI, credentials, network, or gateway service is needed. Input tests use
local pipes; log tests exercise the public JSONL reader against real files.
"""

import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.jitter import JitterConfig, KeystrokeJitterSimulator, sample_jittered_delay, sleep_jittered
from app.response_reader import read_log_records


def record(event):
    return json.dumps(event, ensure_ascii=False).encode("utf-8") + b"\n"


class InputBytesTests(unittest.IsolatedAsyncioTestCase):
    async def test_128k_unicode_paste_survives_pipe_backpressure(self):
        content = "你好😀\n" * (128 * 1024 // 4)
        expected = b"\x1b[200~" + content.encode("utf-8") + b"\x1b[201~\r"
        simulator = KeystrokeJitterSimulator(
            JitterConfig(pre_enter_min_ms=0, pre_enter_max_ms=0)
        )
        read_fd, write_fd = os.pipe()
        os.set_blocking(read_fd, False)
        loop = asyncio.get_running_loop()
        drained = loop.create_future()
        received = bytearray()

        def read_ready():
            try:
                received.extend(os.read(read_fd, 8192))
            except BlockingIOError:
                return
            if len(received) >= len(expected) and not drained.done():
                drained.set_result(None)

        loop.add_reader(read_fd, read_ready)
        try:
            metrics = await asyncio.wait_for(simulator.paste_into_fd(write_fd, content), 5)
            await asyncio.wait_for(drained, 5)
            self.assertEqual(received, expected)
            self.assertEqual(metrics.pasted_chars, 128 * 1024)
            self.assertEqual(metrics.paste_events, 1)
            self.assertTrue(os.get_blocking(write_fd))
        finally:
            loop.remove_reader(read_fd)
            os.close(read_fd)
            os.close(write_fd)

    async def test_bracketed_paste_preserves_multiline_unicode_and_submit(self):
        content = "def example():\n    return '中文 👩‍💻 e\u0301'"
        simulator = KeystrokeJitterSimulator(
            JitterConfig(pre_enter_min_ms=0, pre_enter_max_ms=0)
        )
        read_fd, write_fd = os.pipe()
        try:
            metrics = await simulator.paste_into_fd(write_fd, content)
            self.assertEqual(
                os.read(read_fd, 4096),
                b"\x1b[200~" + content.encode("utf-8") + b"\x1b[201~\r",
            )
            self.assertEqual(metrics.pasted_chars, len(content))
            self.assertEqual(metrics.paste_events, 1)
        finally:
            os.close(read_fd)
            os.close(write_fd)

    async def test_short_unicode_typing_preserves_codepoints_without_submit(self):
        content = "中文 👩‍💻 e\u0301"
        simulator = KeystrokeJitterSimulator(
            JitterConfig(mode="fast", min_delay_ms=0, max_delay_ms=0,
                         distribution="uniform", target_max_cps=1)
        )
        read_fd, write_fd = os.pipe()
        try:
            await simulator.type_into_fd(write_fd, content, append_return=False)
            self.assertEqual(os.read(read_fd, 4096), content.encode("utf-8"))
        finally:
            os.close(read_fd)
            os.close(write_fd)

    def test_sample_jittered_delay_bounds_and_variability(self):
        delays = [sample_jittered_delay(1.0, variance=0.2, distribution="gaussian") for _ in range(50)]
        self.assertTrue(all(d > 0.0 for d in delays))
        self.assertGreater(len(set(round(d, 4) for d in delays)), 10)
        self.assertAlmostEqual(sum(delays) / len(delays), 1.0, delta=0.15)
        # Bounded edge cases
        self.assertEqual(sample_jittered_delay(0.0), 0.0)
        self.assertEqual(sample_jittered_delay(-1.0), 0.0)

    async def test_sleep_jittered_completes_within_expected_window(self):
        start = asyncio.get_running_loop().time()
        await sleep_jittered(0.02, variance=0.1)
        elapsed = asyncio.get_running_loop().time() - start
        self.assertGreaterEqual(elapsed, 0.01)
        self.assertLess(elapsed, 0.1)


class IncrementalLogTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="freebuff-log-test-")
        self.addCleanup(temporary.cleanup)
        self.log_file = Path(temporary.name) / "log.jsonl"
        self.log_file.touch()

    def append(self, payload):
        with self.log_file.open("ab") as stream:
            stream.write(payload)

    def test_complete_json_waits_for_newline_before_consumption(self):
        event = {"msg": "diagnostic", "data": {"detail": "buffered"}}
        self.append(record(event)[:-1])
        position, records = read_log_records(str(self.log_file), 0)
        self.assertEqual((position, records), (0, []))
        self.append(b"\n")
        position, records = read_log_records(str(self.log_file), position)
        self.assertEqual(records, [event])
        self.assertEqual(read_log_records(str(self.log_file), position), (position, []))

    def test_partial_utf8_after_complete_record_is_not_lost_or_repeated(self):
        first = {"msg": "中文"}
        second = {"msg": "👩‍💻"}
        encoded = record(second)
        split_at = encoded.index("👩".encode("utf-8")) + 2
        self.append(record(first) + encoded[:split_at])
        position, records = read_log_records(str(self.log_file), 0)
        self.assertEqual(records, [first])
        self.assertEqual(read_log_records(str(self.log_file), position), (position, []))
        self.append(encoded[split_at:])
        position, records = read_log_records(str(self.log_file), position)
        self.assertEqual(records, [second])
        self.assertEqual(read_log_records(str(self.log_file), position), (position, []))

    def test_unicode_line_separators_inside_json_remain_content(self):
        event = {"msg": "a\u2028b\u2029c\u0085d"}
        self.append(record(event))
        self.assertEqual(read_log_records(str(self.log_file), 0)[1], [event])

    def test_truncated_log_is_read_from_new_start(self):
        self.append(record({"msg": "old" * 300}))
        position, _ = read_log_records(str(self.log_file), 0)
        replacement = {"msg": "new diagnostic"}
        self.log_file.write_bytes(record(replacement))
        position, records = read_log_records(str(self.log_file), position)
        self.assertEqual(records, [replacement])
        self.assertEqual(read_log_records(str(self.log_file), position), (position, []))


if __name__ == "__main__":
    unittest.main()
