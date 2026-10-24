"""Offline metadata-first transcript polling and retry regressions."""

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.response_reader import TranscriptReader, current_response, transcript_signature
from app import response_reader
from app.exceptions import FreebuffTimeoutError
from app.jitter import JitterMetrics
from app.worker import FreebuffWorker


def messages(text="partial", complete=False):
    response = {
        "variant": "ai", "id": "ai-current",
        "blocks": [{"type": "text", "content": text}],
        "isComplete": complete,
    }
    if complete:
        response["metadata"] = {"runState": {"output": {"type": "allMessages"}}}
    return [{"variant": "user", "id": "user-current", "content": "hello"}, response]


class TranscriptPollingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="freebuff-poll-tests-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.clock = self.enterContext(patch("app.response_reader.time.monotonic", return_value=100.0))
        self.reader = TranscriptReader()

    def write_messages(self, value, *, directory=None):
        directory = directory or self.directory
        path = directory / "chat-messages.json"
        temporary = directory / "messages-next.json"
        temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)

    def write_meta(self, *, directory=None):
        directory = directory or self.directory
        stats = (directory / "chat-messages.json").stat()
        value = {"messageCount": 2, "firstPrompt": "hello",
                 "messagesSize": stats.st_size, "messagesMtimeMs": stats.st_mtime_ns / 1_000_000}
        temporary = directory / "meta-next.json"
        temporary.write_text(json.dumps(value), encoding="utf-8")
        temporary.replace(directory / "chat-meta.json")

    def publish(self, value):
        self.write_messages(value)
        self.write_meta()

    def test_large_unchanged_transcript_is_read_once_and_metadata_is_fast_path(self):
        value = messages("x" * (2 * 1024 * 1024))
        self.publish(value)
        with patch("app.response_reader.json.loads", wraps=json.loads) as loads, \
                patch("app.response_reader.transcript_signature", wraps=transcript_signature) as signature:
            self.assertEqual(self.reader.read_changed(self.directory), value)
            for offset in (0.15, 0.30, 0.45, 0.60, 0.75, 0.90):
                self.clock.return_value = 100.0 + offset
                self.assertIsNone(self.reader.read_changed(self.directory))
            self.assertEqual(signature.call_count, 1)
            self.assertEqual(loads.call_count, 1)

            # A sidecar rewrite alone must not deserialize the transcript again.
            self.write_meta()
            self.clock.return_value = 100.95
            self.assertIsNone(self.reader.read_changed(self.directory))
            self.clock.return_value = 102.0
            self.assertIsNone(self.reader.read_changed(self.directory))
            self.assertEqual(loads.call_count, 1)

    def test_same_message_count_observes_stream_growth_and_completion(self):
        for index, value in enumerate((messages("part"), messages("part two"), messages("part two", True))):
            self.publish(value)
            self.clock.return_value = 100.0 + index * 0.15
            observed = self.reader.read_changed(self.directory)
            self.assertEqual(observed, value)
            response = current_response(observed, set(), "hello")
            self.assertEqual(response["complete"], index == 2)
            self.assertEqual(response["content"], value[1]["blocks"][0]["content"])

    def test_stale_or_malformed_metadata_cannot_hide_final_response(self):
        for malformed in (False, True):
            with self.subTest(malformed=malformed):
                self.reader = TranscriptReader()
                self.clock.return_value = 100.0
                self.publish(messages())
                if malformed:
                    (self.directory / "chat-meta.json").write_bytes(b"{broken metadata")
                self.assertEqual(self.reader.read_changed(self.directory), messages())
                self.write_messages(messages("final", True))
                self.clock.return_value = 100.15
                self.assertIsNone(self.reader.read_changed(self.directory))
                self.clock.return_value = 101.01
                self.assertEqual(self.reader.read_changed(self.directory), messages("final", True))

    def test_missing_metadata_checks_transcript_on_each_poll(self):
        self.write_messages(messages())
        self.assertEqual(self.reader.read_changed(self.directory), messages())
        self.write_messages(messages("final", True))
        self.clock.return_value = 100.15
        self.assertEqual(self.reader.read_changed(self.directory), messages("final", True))

    def test_delayed_metadata_wakes_polling_before_fallback_deadline(self):
        self.publish(messages())
        self.reader.read_changed(self.directory)
        self.write_messages(messages("final", True))
        self.clock.return_value = 100.15
        self.assertIsNone(self.reader.read_changed(self.directory))
        self.write_meta()
        self.clock.return_value = 100.30
        self.assertEqual(self.reader.read_changed(self.directory), messages("final", True))

    def test_metadata_ahead_of_transcript_does_not_hide_later_write(self):
        self.publish(messages())
        self.reader.read_changed(self.directory)
        self.write_meta()
        self.clock.return_value = 100.15
        self.assertIsNone(self.reader.read_changed(self.directory))
        self.write_messages(messages("final", True))
        self.clock.return_value = 100.30
        self.assertIsNone(self.reader.read_changed(self.directory))
        self.clock.return_value = 101.16
        self.assertEqual(self.reader.read_changed(self.directory), messages("final", True))

    def test_atomic_replacement_with_same_size_and_mtime_is_still_read(self):
        self.publish(messages("first"))
        self.reader.read_changed(self.directory)
        path = self.directory / "chat-messages.json"
        original = path.stat()
        self.write_messages(messages("other"))
        os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
        self.assertEqual(path.stat().st_size, original.st_size)
        self.assertNotEqual(path.stat().st_ino, original.st_ino)
        self.write_meta()
        self.clock.return_value = 100.15
        self.assertEqual(self.reader.read_changed(self.directory), messages("other"))

    def test_incomplete_transcripts_retry_without_another_metadata_change(self):
        final = messages("你好", True)
        encoded = json.dumps(final, ensure_ascii=False).encode("utf-8")
        split_at = encoded.index("你".encode("utf-8")) + 1
        for raw in (None, b"[{", encoded[:split_at]):
            with self.subTest(raw=raw):
                self.reader = TranscriptReader()
                self.clock.return_value = 100.0
                self.publish(messages())
                self.reader.read_changed(self.directory)
                # Signal an update, then simulate a missing or half-written file.
                self.write_meta()
                path = self.directory / "chat-messages.json"
                if raw is None:
                    path.unlink()
                else:
                    path.write_bytes(raw)
                self.clock.return_value = 100.15
                self.assertIsNone(self.reader.read_changed(self.directory))
                self.write_messages(final)
                self.clock.return_value = 100.30
                self.assertEqual(self.reader.read_changed(self.directory), final)

    def test_directory_change_discards_previous_cache(self):
        self.publish(messages("first"))
        self.reader.read_changed(self.directory)
        other = self.directory / "other-chat"
        other.mkdir()
        self.write_messages(messages("other"), directory=other)
        self.write_meta(directory=other)
        self.clock.return_value = 100.15
        self.assertEqual(self.reader.read_changed(other), messages("other"))
        self.clock.return_value = 100.30
        self.assertEqual(self.reader.read_changed(self.directory), messages("first"))

    def test_valid_empty_transcript_is_cached_as_successful_read(self):
        self.publish([])
        with patch("app.response_reader.json.loads", wraps=json.loads) as loads:
            self.assertEqual(self.reader.read_changed(self.directory), [])
            self.clock.return_value = 100.15
            self.assertIsNone(self.reader.read_changed(self.directory))
            self.assertEqual(loads.call_count, 1)


class WorkerTranscriptPollingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="freebuff-worker-poll-tests-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.worker = FreebuffWorker(workdir=str(self.root / "workspace"),
                                     home_dir=str(self.root / "account"))
        self.worker.is_running = True
        self.worker.master_fd = -1
        self.worker.ready_event.set()
        self.worker.stop = AsyncMock()
        self.worker.jitter_simulator.type_into_fd = AsyncMock(return_value=JitterMetrics())
        self.worker.jitter_simulator.paste_into_fd = AsyncMock(return_value=JitterMetrics())
        self.chats = Path(self.worker.config_dir) / "projects" / self.worker.project_basename / "chats"
        self.current = self.chats / "old"
        self.current.mkdir(parents=True)
        self.publish(messages("x" * (2 * 1024 * 1024), True))
        self.worker._get_project_chat_dir = Mock(side_effect=lambda: str(self.current))

    def publish(self, value):
        path = self.current / "chat-messages.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        stat = path.stat()
        meta = self.current / "meta-next.json"
        meta.write_text(json.dumps({"messageCount": len(value), "firstPrompt": "hello",
                                    "messagesSize": stat.st_size,
                                    "messagesMtimeMs": stat.st_mtime_ns / 1_000_000}))
        meta.replace(self.current / "chat-meta.json")

    async def test_new_chat_never_reads_old_large_transcript(self):
        old = str(self.current)

        async def reset(_fd, command, append_return=True):
            self.assertEqual(command, "/new")
            self.current = self.chats / "fresh"
            self.current.mkdir()
            self.publish([])
            return JitterMetrics()

        async def submit(_fd, prompt, append_return=True):
            value = messages("fresh answer", True)
            value[0]["content"] = prompt
            self.publish(value)
            return JitterMetrics()

        self.worker.jitter_simulator.type_into_fd.side_effect = reset
        self.worker.jitter_simulator.paste_into_fd.side_effect = submit
        chunks = []
        with patch.object(response_reader, "_read_messages", wraps=response_reader._read_messages) as read, \
                patch("app.worker.TURN_TIMEOUT_SECONDS", 2):
            result = await self.worker.execute_turn("hello", on_chunk=lambda *chunk: chunks.append(chunk))
        self.assertNotIn(old, [str(call.args[0]) for call in read.call_args_list])
        self.assertEqual(result["content"], "fresh answer")
        self.assertEqual(result["chat_dir"], str(self.current))
        self.assertEqual(chunks, [("fresh answer", False)])
        self.worker.stop.assert_not_awaited()

    async def test_reused_chat_does_not_reparse_unchanged_baseline(self):
        with patch.object(response_reader, "_read_messages", wraps=response_reader._read_messages) as read, \
                patch("app.worker.TURN_TIMEOUT_SECONDS", 0.4):
            with self.assertRaises(FreebuffTimeoutError):
                await self.worker.execute_turn("hello", reset_context=False)
        self.assertGreaterEqual(self.worker._get_project_chat_dir.call_count, 3)
        read.assert_called_once_with(str(self.current))
        self.worker.stop.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
