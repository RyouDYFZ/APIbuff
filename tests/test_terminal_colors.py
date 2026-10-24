"""Offline OSC color negotiation over real local PTYs; no CLI or network."""

import asyncio
import os
from pathlib import Path
import pty
import sys
import tempfile
import tty
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.jitter import write_all
from app.worker import FreebuffWorker


BACKGROUND = b"\x1b]11;rgb:0000/0000/0000\x07"
FOREGROUND = b"\x1b]10;rgb:ffff/ffff/ffff\x07"
QUERIES = tuple(
    (b"\x1b]" + code + b";?" + terminator, response)
    for code, response in ((b"10", FOREGROUND), (b"11", BACKGROUND))
    for terminator in (b"\x07", b"\x1b\\")
)


class TerminalColorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="freebuff-osc-test-")
        self.addCleanup(temporary.cleanup)
        self.worker = FreebuffWorker(
            workdir=temporary.name,
            home_dir=str(Path(temporary.name) / "account"),
        )
        self.worker._loop = asyncio.get_running_loop()
        self.slave = None
        self.open_terminal()

    def open_terminal(self):
        master, self.slave = pty.openpty()
        tty.setraw(self.slave)
        os.set_blocking(master, False)
        os.set_blocking(self.slave, False)
        self.worker.master_fd = master
        self.worker.is_running = True

    async def asyncTearDown(self):
        try:
            await self.worker.stop()
        finally:
            if self.slave is not None:
                os.close(self.slave)

    async def readable(self, fd):
        loop = asyncio.get_running_loop()
        ready = loop.create_future()

        def wake():
            if not ready.done():
                ready.set_result(None)

        loop.add_reader(fd, wake)
        try:
            await asyncio.wait_for(ready, timeout=1)
        finally:
            loop.remove_reader(fd)

    async def deliver(self, output):
        """Deliver exactly one output fragment before processing the next."""
        self.assertEqual(os.write(self.slave, output), len(output))
        await self.readable(self.worker.master_fd)
        self.worker._on_pty_readable()

    async def feed(self, output):
        await self.deliver(output)
        await asyncio.gather(*tuple(self.worker._reply_tasks))

    def read_input(self):
        response = bytearray()
        while True:
            try:
                chunk = os.read(self.slave, 8192)
            except BlockingIOError:
                break
            if not chunk:
                break
            response.extend(chunk)
        return bytes(response)

    async def test_each_query_split_at_every_byte_boundary(self):
        for query, response in QUERIES:
            for split in range(1, len(query)):
                with self.subTest(query=query, split=split):
                    await self.feed(query[:split])
                    self.assertEqual(self.read_input(), b"")
                    await self.feed(query[split:])
                    self.assertEqual(self.read_input(), response)

    async def test_queries_can_arrive_one_byte_at_a_time(self):
        for query, response in QUERIES:
            with self.subTest(query=query):
                for byte in query[:-1]:
                    await self.feed(bytes([byte]))
                    self.assertEqual(self.read_input(), b"")
                await self.feed(query[-1:])
                self.assertEqual(self.read_input(), response)

    async def test_mixed_and_repeated_queries_reply_once_in_output_order(self):
        await self.feed(
            b"\x1b]10;?\x07\x1b]11;?\x1b\\"
            b"\x1b]11;?\x07\x1b]10;?\x1b\\\x1b]11;?\x07"
        )
        self.assertEqual(
            self.read_input(),
            FOREGROUND + BACKGROUND + BACKGROUND + FOREGROUND + BACKGROUND,
        )

    async def test_waits_for_complete_terminator(self):
        await self.feed(b"\x1b]11;?")
        self.assertEqual(self.read_input(), b"")
        await self.feed(b"\x1b")
        self.assertEqual(self.read_input(), b"")
        await self.feed(b"\\")
        self.assertEqual(self.read_input(), BACKGROUND)

    async def test_other_sequences_and_color_settings_do_not_query(self):
        output = (
            b"OSC 11;?\x07"
            b"\x1b]0;terminal title\x07"
            b"\x1b]12;?\x07\x1b]110;?\x1b\\"
            b"\x1b]10;rgb:ffff/ffff/ffff\x07"
            b"\x1b]11;#000000\x1b\\"
            b"\x1b]11;?invalid\x07"
            b"\x1b]11;?\x1b[0m"
        )
        await self.feed(output)
        self.assertEqual(self.read_input(), b"")
        await self.feed(b"\x1b]11;?\x07")
        self.assertEqual(self.read_input(), BACKGROUND)

    async def test_later_output_does_not_replay_query_or_change_raw_output(self):
        before = b"\x1b[31mboot\x1b[0m\x1b]11;?\x07"
        after = b"\r\nEnter a coding task"
        await self.feed(before)
        self.assertEqual(self.read_input(), BACKGROUND)
        await self.feed(after)
        self.assertEqual(self.read_input(), b"")
        self.assertEqual(bytes(self.worker._raw_buffer), before + after)
        self.assertTrue(self.worker.ready_event.is_set())

    async def test_query_survives_raw_buffer_rollover(self):
        # Small writes also fit macOS's PTY output queue without a writer task.
        for _ in range(70):
            await self.feed(b"x" * 1000)
        await self.feed(b"\x1b]1")
        self.assertEqual(self.read_input(), b"")
        await self.feed(b"1;?\x07")
        self.assertEqual(self.read_input(), BACKGROUND)
        self.assertEqual(len(self.worker._raw_buffer), 65536)
        self.assertTrue(self.worker._raw_buffer.endswith(b"\x1b]11;?\x07"))

    async def test_new_terminal_does_not_complete_previous_terminal_query(self):
        await self.feed(b"\x1b]11;")
        self.assertEqual(self.read_input(), b"")
        self.worker._close_terminal()
        os.close(self.slave)
        self.slave = None
        self.open_terminal()
        await self.feed(b"?\x07")
        self.assertEqual(self.read_input(), b"")
        await self.feed(b"\x1b]11;?\x07")
        self.assertEqual(self.read_input(), BACKGROUND)

    async def test_stop_cancels_queued_reply_tasks(self):
        os.write(self.slave, b"\x1b]11;?\x07")
        await self.readable(self.worker.master_fd)
        self.worker._on_pty_readable()
        replies = tuple(self.worker._reply_tasks)
        self.assertTrue(replies)
        await self.worker.stop()
        self.assertTrue(all(task.done() for task in replies))
        self.assertFalse(self.worker._reply_tasks)
        self.assertIsNone(self.worker.master_fd)

    async def test_backpressure_does_not_interleave_replies_from_later_reads(self):
        first_started = asyncio.Event()
        resume = asyncio.Event()

        async def pause_mid_reply(fd, payload):
            if not first_started.is_set():
                await write_all(fd, payload[:8])
                first_started.set()
                await resume.wait()
                await write_all(fd, payload[8:])
            else:
                await write_all(fd, payload)

        with patch("app.worker.write_all", side_effect=pause_mid_reply):
            try:
                await self.deliver(b"\x1b]11;?\x07")
                await asyncio.wait_for(first_started.wait(), timeout=1)
                await self.deliver(b"\x1b]10;?\x07")
                # Give the second reply a chance to run while the first is full.
                await asyncio.sleep(0)
                self.assertEqual(self.read_input(), BACKGROUND[:8])
                resume.set()
                await asyncio.wait_for(
                    asyncio.gather(*tuple(self.worker._reply_tasks)), timeout=1
                )
                self.assertEqual(self.read_input(), BACKGROUND[8:] + FOREGROUND)
            finally:
                resume.set()

    async def test_close_cancels_stalled_replies_before_terminal_reuse(self):
        started = asyncio.Event()
        resume = asyncio.Event()

        async def stalled_write(fd, payload):
            started.set()
            await resume.wait()
            await write_all(fd, payload)

        with patch("app.worker.write_all", side_effect=stalled_write):
            try:
                await self.deliver(b"\x1b]11;?\x07")
                await asyncio.wait_for(started.wait(), timeout=1)
                await self.deliver(b"\x1b]10;?\x07")
                await asyncio.sleep(0)
                replies = tuple(self.worker._reply_tasks)
                self.worker._close_terminal()
                os.close(self.slave)
                self.slave = None
                self.open_terminal()
                resume.set()
                await asyncio.wait_for(
                    asyncio.gather(*replies, return_exceptions=True), timeout=1
                )
                self.assertEqual(self.read_input(), b"")
                self.assertTrue(all(task.cancelled() for task in replies))
                self.assertFalse(self.worker._reply_tasks)
            finally:
                resume.set()
        await self.feed(b"\x1b]11;?\x07")
        self.assertEqual(self.read_input(), BACKGROUND)

    async def test_event_loop_delivers_reply_before_cli_ready_prompt(self):
        self.worker._loop.add_reader(
            self.worker.master_fd, self.worker._on_pty_readable
        )
        os.write(self.slave, b"\x1b]11;?\x07")
        await self.readable(self.slave)
        self.assertEqual(self.read_input(), BACKGROUND)
        self.assertFalse(self.worker.ready_event.is_set())
        os.write(self.slave, b"Enter a coding task")
        await asyncio.wait_for(self.worker._wait_for_ready(), timeout=1)
        self.assertTrue(self.worker.ready_event.is_set())


if __name__ == "__main__":
    unittest.main()
