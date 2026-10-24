"""Offline, time-bounded regression tests for diagnostics and terminal byte writes."""

import asyncio
import errno
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.jitter import JitterConfig, KeystrokeJitterSimulator, write_all
from tests.test_chat_protocol import ASGIExchange
from types import SimpleNamespace

PROJECT = Path(__file__).resolve().parent.parent


class OfflineClient:
    async def post(self, path, *, json=None, content=None, headers=None):
        return await self.request(path, method="POST", payload=json, content=content, headers=headers)

    async def get(self, path):
        return await self.request(path, method="GET")

    async def request(self, path, *, method, payload=None, content=None, headers=None):
        exchange = ASGIExchange(payload, method=method, path=path, headers=headers)
        if content is not None:
            exchange.incoming.get_nowait()
            exchange.incoming.put_nowait({"type": "http.request", "body": content.encode(), "more_body": False})
        await exchange.run()
        return SimpleNamespace(status_code=exchange.status, text=exchange.body.decode(), json=lambda: exchange.json)


class JitterConfigurationTests(unittest.TestCase):
    def test_invalid_configuration_is_rejected_before_typing(self):
        for fields in (
            {"target_max_cps": 0.5}, {"target_max_cps": 0},
            {"target_max_cps": float("nan")}, {"target_max_cps": float("inf")},
            {"min_delay_ms": -1}, {"min_delay_ms": 100, "max_delay_ms": 1},
            {"pre_enter_min_ms": 20, "pre_enter_max_ms": 10},
            {"max_delay_ms": 5001}, {"pre_enter_max_ms": float("inf")},
            {"mode": "unrecognized"}, {"distribution": "unrecognized"},
        ):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                JitterConfig(**fields)


class TerminalWriteTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_writes_eagain_and_eintr_preserve_all_utf8_bytes(self):
        payload = "中文 👩‍💻 e\u0301\n".encode("utf-8")
        accepted = bytearray()
        call_count = 0
        read_fd, write_fd = os.pipe()
        def short_write(fd, data):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise BlockingIOError(errno.EAGAIN, "temporarily full")
            if call_count == 3:
                raise InterruptedError(errno.EINTR, "signal")
            size = min(2, len(data))
            accepted.extend(data[:size])
            return size
        try:
            with patch("app.jitter.os.write", side_effect=short_write):
                await asyncio.wait_for(write_all(write_fd, payload), 1)
            self.assertEqual(bytes(accepted), payload)
            self.assertTrue(os.get_blocking(write_fd))
        finally:
            os.close(read_fd)
            os.close(write_fd)

    async def test_cancel_full_pipe_writer_returns_promptly(self):
        read_fd, write_fd = os.pipe()
        os.set_blocking(write_fd, False)
        try:
            while True:
                try:
                    os.write(write_fd, b"x" * 4096)
                except BlockingIOError:
                    break
            task = asyncio.create_task(write_all(write_fd, b"next"))
            await asyncio.sleep(0.01)
            self.assertFalse(task.done())
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 0.25)
            self.assertFalse(os.get_blocking(write_fd))
        finally:
            os.close(read_fd)
            os.close(write_fd)

    async def test_zero_progress_write_fails_instead_of_spinning(self):
        with tempfile.TemporaryFile() as capture:
            with patch("app.jitter.os.write", return_value=0):
                with self.assertRaisesRegex(OSError, "no progress"):
                    await asyncio.wait_for(write_all(capture.fileno(), b"payload"), 0.25)

    async def test_zero_delay_poisson_is_valid_and_keeps_unicode_complete(self):
        simulator = KeystrokeJitterSimulator(JitterConfig(
            mode="human", distribution="poisson", min_delay_ms=0, max_delay_ms=0,
            pre_enter_min_ms=0, pre_enter_max_ms=0, target_max_cps=1000,
        ))
        content = "中文🙂"
        with tempfile.TemporaryFile() as capture:
            await asyncio.wait_for(simulator.type_into_fd(capture.fileno(), content), 1)
            capture.seek(0)
            self.assertEqual(capture.read(), content.encode("utf-8") + b"\r")

    async def test_mutated_unsafe_configuration_is_revalidated(self):
        config = JitterConfig()
        config.target_max_cps = 0.5
        with tempfile.TemporaryFile() as capture:
            with self.assertRaises(ValueError):
                await asyncio.wait_for(KeystrokeJitterSimulator(config).type_into_fd(capture.fileno(), "x"), 0.25)

    async def test_real_backpressure_does_not_block_timer_in_bounded_subprocess(self):
        # An outer process timeout also catches regressions that block asyncio
        # itself, for which an in-loop wait_for would never get to run.
        source = textwrap.dedent("""
            import asyncio, os
            from app.jitter import write_all
            async def run():
                read_fd, write_fd = os.pipe()
                payload = ('中文🙂' * 40000).encode('utf-8')
                try:
                    writer = asyncio.create_task(write_all(write_fd, payload))
                    timer_fired = asyncio.Event()
                    asyncio.get_running_loop().call_later(0.02, timer_fired.set)
                    await asyncio.wait_for(timer_fired.wait(), 0.25)
                    assert not writer.done(), 'large write should be waiting for its reader'
                    def read_all():
                        content = bytearray()
                        while len(content) < len(payload):
                            content.extend(os.read(read_fd, 8192))
                        return bytes(content)
                    _, captured = await asyncio.wait_for(asyncio.gather(writer, asyncio.to_thread(read_all)), 2)
                    assert captured == payload
                finally:
                    os.close(read_fd)
                    os.close(write_fd)
            asyncio.run(run())
        """)
        result = await asyncio.to_thread(subprocess.run, [sys.executable, "-c", source], cwd=PROJECT, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)


class DiagnosticRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = OfflineClient()
        key = patch("app.main.API_KEY", "")
        key.start()
        self.addCleanup(key.stop)

    async def test_invalid_numeric_modes_ranges_and_lengths_return_422(self):
        for fields in (
            {"target_max_cps": 0.5}, {"target_max_cps": 0}, {"target_max_cps": "NaN"},
            {"min_delay_ms": -1}, {"max_delay_ms": 6000},
            {"min_delay_ms": 100, "max_delay_ms": 1},
            {"pre_enter_min_ms": 100, "pre_enter_max_ms": 1},
            {"mode": "invalid"}, {"distribution": "invalid"},
            {"text": "x" * 129, "mode": "human"},
            {"text": "x" * 4097, "mode": "instant"},
        ):
            with self.subTest(fields=str(fields)[:100]):
                response = await asyncio.wait_for(self.client.post("/v1/system/jitter-test", json=fields), 1)
                self.assertEqual(response.status_code, 422, response.text)
        response = await self.client.post("/v1/system/paste-test", json={"text": "x" * 4097})
        self.assertEqual(response.status_code, 422)
        for literal in ("NaN", "Infinity", "-Infinity"):
            response = await self.client.post("/v1/system/jitter-test", content='{"target_max_cps": ' + literal + '}', headers={"Content-Type": "application/json"})
            self.assertEqual(response.status_code, 422)

    async def test_local_typing_reports_complete_bytes_and_simulation_scope(self):
        content = "中文 👩‍💻 e\u0301"
        response = await asyncio.wait_for(self.client.post("/v1/system/jitter-test", json={"text": content, "mode": "instant"}), 1)
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["captured_bytes_len"], len(content.encode("utf-8")) + 1)
        self.assertEqual(result["measurement_scope"], "local_input_simulation")
        self.assertFalse(result["cps_limit_applied"])

    async def test_paste_honors_requested_pause_and_preserves_brackets(self):
        response = await asyncio.wait_for(self.client.post("/v1/system/paste-test", json={
            "text": "line one\n中文", "pre_enter_min_ms": 0, "pre_enter_max_ms": 0,
        }), 1)
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertTrue(result["contains_bracketed_paste_seq"])
        self.assertEqual(result["pre_enter_ms"], 0)
        self.assertEqual(result["captured_bytes_len"], len("line one\n中文".encode("utf-8")) + 13)

    async def test_diagnostic_deadline_cancels_simulation_and_returns_504(self):
        cancelled = asyncio.Event()
        async def never_finishes(*args, **kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        with patch("app.routes.system.DIAGNOSTIC_TIMEOUT_SECONDS", 0.025), patch("app.routes.system.KeystrokeJitterSimulator.type_into_fd", side_effect=never_finishes):
            response = await asyncio.wait_for(self.client.post("/v1/system/jitter-test", json={"text": "short"}), 1)
        self.assertEqual(response.status_code, 504, response.text)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(response.json()["error"].get("code"), "diagnostic_timeout")

    async def test_large_unicode_diagnostics_cannot_block_loop_in_bounded_process(self):
        source = textwrap.dedent("""
            import asyncio
            from tests.test_diagnostics import OfflineClient
            import app.main as main
            main.API_KEY = ''
            async def run():
                content = '🙂' * 4096
                client = OfflineClient()
                for path in ('jitter-test', 'paste-test'):
                    result = await asyncio.wait_for(client.post('/v1/system/' + path, json={
                        'text': content, 'mode': 'instant', 'pre_enter_min_ms': 0, 'pre_enter_max_ms': 0
                    }), 2)
                    assert result.status_code == 200, result.text
                    expected = len(content.encode('utf-8')) + (13 if path == 'paste-test' else 1)
                    assert result.json()['captured_bytes_len'] == expected
            asyncio.run(run())
        """)
        result = await asyncio.to_thread(subprocess.run, [sys.executable, "-c", source], cwd=PROJECT, capture_output=True, text=True, timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr)

    async def test_environment_audit_subprocess_inspection_is_off_event_loop(self):
        main_thread = threading.get_ident()
        threads = []
        worker = MagicMock(is_running=True)
        worker.ready_event.is_set.return_value = True
        def pid():
            threads.append(threading.get_ident())
            time.sleep(0.025)
            return 12345
        def audit(pid):
            threads.append(threading.get_ident())
            time.sleep(0.025)
            return {"inspected_pid": pid}
        worker.get_freebuff_pid.side_effect = pid
        with patch("app.routes.system.global_pool") as pool, patch("app.routes.system.audit_runtime_environment", side_effect=audit):
            pool.worker = worker
            response = await asyncio.wait_for(self.client.get("/v1/system/env-audit"), 1)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["inspected_pid"], 12345)
        self.assertTrue(all(thread != main_thread for thread in threads))

    async def test_workspace_switch_uses_pool_gate_and_git_info_is_offloaded(self):
        main_thread = threading.get_ident()
        threads = []
        def git_info(path):
            threads.append(threading.get_ident())
            return {"path": path}
        with patch("app.routes.system.global_pool") as pool, patch("app.routes.system.get_workspace_git_info", side_effect=git_info):
            pool.set_workspace = AsyncMock(return_value="/tmp/canonical-workspace")
            pool.worker.workdir = "/tmp/current-workspace"
            switched = await self.client.post("/v1/workspace", json={"path": "/tmp/requested-workspace"})
            current = await self.client.get("/v1/workspace")
            pool.set_workspace.assert_awaited_once_with("/tmp/requested-workspace")
            pool.worker.ensure_workspace.assert_not_called()
        self.assertEqual(switched.status_code, 200)
        self.assertEqual(switched.json()["active_workspace"], "/tmp/canonical-workspace")
        self.assertEqual(current.status_code, 200)
        self.assertEqual(len(threads), 2)
        self.assertTrue(all(thread != main_thread for thread in threads))


if __name__ == "__main__":
    unittest.main()
