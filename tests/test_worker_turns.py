"""Offline public-transcript correlation and worker failure-cleanup regressions.

Fake CLI input callbacks write only temporary account chat files. No installed
CLI, upstream service, credentials, or user's prompt files are accessed.
"""

import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.exceptions import (
    FreebuffException, FreebuffAuthError, FreebuffBadRequestError,
    FreebuffPaymentError, FreebuffPermissionError, FreebuffRateLimitError,
    FreebuffServerError, FreebuffTimeoutError, FreebuffInteractionRequiredError,
)
from app.jitter import JitterMetrics
from app.response_reader import current_response, read_messages, check_log_error
from app.worker import FreebuffWorker, build_wire_prompt, validate_turn_prompt
from app.pool import MultiAccountWorkerPool
from app.models import ChatMessage


WIRE_HELLO = "[Current Request]\nhello"


class WirePromptTests(unittest.TestCase):
    def test_serialized_history_is_not_prefixed_twice(self):
        pool = MultiAccountWorkerPool()
        messages = [
            ChatMessage(role="system", content="be brief"),
            ChatMessage(role="user", content="first question"),
            ChatMessage(role="assistant", content="first answer"),
            ChatMessage(role="user", content="current question"),
        ]
        serialized = validate_turn_prompt(pool.format_messages(messages, is_new_session=True))
        wire = build_wire_prompt(serialized)
        self.assertEqual(wire.count("[Current Request]"), 1)
        # The serialized form already carries its own section markers.
        self.assertEqual(wire, serialized)
        self.assertTrue(wire.startswith("[System Instructions]"))
        # Raw prompts (daemon path) still get exactly one marker.
        self.assertEqual(build_wire_prompt("hello"), WIRE_HELLO)


def user(message_id="user-new", prompt=WIRE_HELLO):
    return {"variant": "user", "id": message_id, "content": prompt}


def answer(message_id="ai-new", text="fresh answer", complete=True, **extra):
    return {
        "variant": "ai", "id": message_id,
        "blocks": [{"type": "text", "content": text}],
        "isComplete": complete,
        **({"metadata": {"runState": {"output": {"type": "allMessages", "value": []}}}} if complete else {}),
        **extra,
    }


def log_record(record):
    return json.dumps(record, ensure_ascii=False).encode("utf-8") + b"\n"


class WorkerTurnTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="freebuff-turn-tests-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workdir = self.root / "workspace"
        self.workdir.mkdir()
        self.worker = FreebuffWorker(workdir=str(self.workdir), home_dir=str(self.root / "account"))
        self.worker.is_running = True
        self.worker.master_fd = -1  # All terminal input is replaced with fake CLI callbacks.
        self.worker.ready_event.set()
        self.worker.jitter_simulator.type_into_fd = AsyncMock(return_value=JitterMetrics())
        self.worker.jitter_simulator.paste_into_fd = AsyncMock(return_value=JitterMetrics())
        self.sequence = 0
        self.old = self.create_chat([user("user-old"), answer("ai-old", "old answer")])
        self.history_path = Path(self.worker.config_dir) / "message-history.json"

        async def reset(_fd, command, append_return=True):
            self.assertEqual(command, "/new")
            self.assertTrue(append_return)
            history = json.loads(self.history_path.read_text()) if self.history_path.exists() else []
            self.history_path.write_text(json.dumps([*history, command]))
            return JitterMetrics()

        self.worker.jitter_simulator.type_into_fd.side_effect = reset

        async def stop_fake():
            self.worker.is_running = False
            self.worker.master_fd = None

        self.worker.stop = AsyncMock(side_effect=stop_fake)

    def create_chat(self, messages, logs=b"", metadata=None):
        self.sequence += 1
        directory = Path(self.worker.config_dir) / "projects" / self.worker.project_basename / "chats" / f"chat-{self.sequence:03d}"
        directory.mkdir(parents=True)
        self.write_messages(directory, messages)
        (directory / "log.jsonl").write_bytes(logs)
        if metadata is not None:
            (directory / "chat-meta.json").write_text(json.dumps(metadata))
        # Deterministic newest-chat ordering, independent of filesystem precision.
        os.utime(directory, (1_700_000_000 + self.sequence, 1_700_000_000 + self.sequence))
        return directory

    def write_messages(self, directory, messages):
        path = directory / "chat-messages.json"
        path.write_text(json.dumps(messages, ensure_ascii=False), encoding="utf-8")
        # The CLI publishes its summary after every transcript checkpoint,
        # including updates to the same assistant message.
        stat = path.stat()
        (directory / "chat-meta.json").write_text(json.dumps({
            "messageCount": len(messages), "firstPrompt": "hello",
            "messagesSize": stat.st_size, "messagesMtimeMs": stat.st_mtime_ns / 1_000_000,
        }))

    def on_submit(self, callback):
        async def paste(_fd, command, append_return=True):
            callback(command)
            return JitterMetrics()
        self.worker.jitter_simulator.paste_into_fd.side_effect = paste

    async def execute(self, prompt="hello", *, reset=True, timeout=1, on_chunk=None):
        with patch("app.worker.TURN_TIMEOUT_SECONDS", timeout), patch("app.worker.TAKEOVER_AUTO_CONFIRM", False):
            return await self.worker.execute_turn(prompt, reset_context=reset, on_chunk=on_chunk)

    async def test_reset_requires_fresh_directory_and_does_not_return_old_answer(self):
        self.on_submit(lambda _command: self.write_messages(self.old, [
            user("user-old"), answer("ai-old", "old answer"), user(), answer(),
        ]))
        with self.assertRaises(FreebuffTimeoutError):
            await self.execute(timeout=0.08)
        self.worker.stop.assert_awaited_once()

    async def test_old_answer_and_terminal_output_cannot_be_timeout_fallback(self):
        self.on_submit(lambda _command: self.worker._raw_buffer.extend(b"plausible answer\nAdd to the current task"))
        emitted = []
        with self.assertRaises(FreebuffTimeoutError):
            await self.execute(reset=False, timeout=0.08, on_chunk=lambda text, reasoning: emitted.append((text, reasoning)))
        self.assertEqual(emitted, [])
        self.worker.stop.assert_awaited_once()

    async def test_same_prompt_requires_new_user_and_answer_ids(self):
        self.on_submit(lambda _command: self.write_messages(self.old, [
            user("user-old"), answer("ai-old", "old answer"), user("user-new"), answer("ai-new", "current answer"),
        ]))
        result = await self.execute(reset=False)
        self.assertEqual(result["content"], "current answer")
        self.assertEqual(result["message_id"], "ai-new")
        self.worker.stop.assert_not_awaited()

    async def test_reusing_old_user_id_does_not_make_a_fresh_turn(self):
        self.on_submit(lambda _command: self.write_messages(self.old, [user("user-old"), answer("ai-new", "unrelated answer")]))
        with self.assertRaises(FreebuffTimeoutError):
            await self.execute(reset=False, timeout=0.08)

    async def test_reusing_old_ai_id_does_not_make_a_fresh_response(self):
        self.on_submit(lambda _command: self.write_messages(self.old, [user(), answer("ai-old", "reused answer")]))
        with self.assertRaises(FreebuffTimeoutError):
            await self.execute(reset=False, timeout=0.08)

    async def test_new_user_with_different_prompt_is_not_accepted(self):
        self.on_submit(lambda _command: self.create_chat([user(prompt="someone else's prompt"), answer()]))
        with self.assertRaises(FreebuffTimeoutError):
            await self.execute(timeout=0.08)

    async def test_reset_multiline_prompt_is_pasted_separately_after_reset(self):
        prompt = "def hello():\n    return '你好 👩‍💻'"
        self.on_submit(lambda command: self.create_chat([user(prompt=command), answer()]))
        result = await self.execute(prompt)
        self.assertEqual(result["content"], "fresh answer")
        self.worker.jitter_simulator.paste_into_fd.assert_awaited_once_with(-1, "[Current Request]\n" + prompt, append_return=True)
        self.worker.jitter_simulator.type_into_fd.assert_awaited_once_with(-1, "/new", append_return=True)

    async def test_fresh_directory_acknowledges_reset_without_history_file(self):
        state = {}
        async def reset(_fd, command, append_return=True):
            state["chat"] = self.create_chat([])
            return JitterMetrics()
        self.worker.jitter_simulator.type_into_fd.side_effect = reset
        self.on_submit(lambda command: self.write_messages(state["chat"], [user(prompt=command), answer()]))
        result = await self.execute()
        self.assertEqual(result["chat_dir"], str(state["chat"]))
        self.assertFalse(self.history_path.exists())

    async def test_old_reset_record_cannot_acknowledge_a_new_reset(self):
        self.history_path.write_text(json.dumps(["/new"]))
        self.worker.jitter_simulator.type_into_fd.side_effect = None
        with self.assertRaises(FreebuffTimeoutError):
            await self.execute(timeout=0.08)
        self.worker.jitter_simulator.paste_into_fd.assert_not_awaited()
        self.worker.stop.assert_awaited_once()

    async def test_partial_reset_history_waits_before_pasting(self):
        reset_seen = asyncio.Event()
        async def reset(_fd, command, append_return=True):
            self.history_path.write_text('["/new"')
            reset_seen.set()
            return JitterMetrics()
        self.worker.jitter_simulator.type_into_fd.side_effect = reset
        self.on_submit(lambda command: self.create_chat([user(prompt=command), answer()]))
        task = asyncio.create_task(self.execute())
        try:
            await asyncio.wait_for(reset_seen.wait(), 0.5)
            await asyncio.sleep(0.18)
            self.worker.jitter_simulator.paste_into_fd.assert_not_awaited()
            self.history_path.write_text(json.dumps(["/new"]))
            self.assertEqual((await task)["content"], "fresh answer")
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_unrelated_history_change_cannot_acknowledge_reset(self):
        async def reset(_fd, command, append_return=True):
            self.history_path.write_text(json.dumps(["/new", "some other input"]))
            return JitterMetrics()
        self.worker.jitter_simulator.type_into_fd.side_effect = reset
        with self.assertRaises(FreebuffTimeoutError):
            await self.execute(timeout=0.08)
        self.worker.jitter_simulator.paste_into_fd.assert_not_awaited()

    async def test_cancellation_while_reset_pending_does_not_paste_prompt(self):
        reset_seen = asyncio.Event()
        async def reset(_fd, command, append_return=True):
            reset_seen.set()
            return JitterMetrics()
        self.worker.jitter_simulator.type_into_fd.side_effect = reset
        task = asyncio.create_task(self.execute())
        await asyncio.wait_for(reset_seen.wait(), 0.5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.worker.jitter_simulator.paste_into_fd.assert_not_awaited()
        self.worker.stop.assert_awaited_once()
        self.assertFalse(self.worker.turn_lock.locked())

    async def test_command_and_file_like_prompts_are_sent_as_text(self):
        for prompt in ("/help", "!echo test", "init", "logout", str(self.old / "chat-messages.json")):
            with self.subTest(prompt=prompt):
                self.on_submit(lambda command: self.create_chat([user(prompt=command), answer()]))
                result = await self.execute(prompt)
                self.worker.jitter_simulator.paste_into_fd.assert_awaited_with(-1, "[Current Request]\n" + prompt, append_return=True)
                self.assertEqual(result["content"], "fresh answer")

    async def test_internal_completion_logs_and_metadata_do_not_finish_partial_public_answer(self):
        emitted = []
        seen_partial = asyncio.Event()
        state = {}

        def submit(_command):
            state["chat"] = self.create_chat(
                [user(), answer(text="partial", complete=False)],
                logs=log_record({"msg": "End agent", "data": {"agentId": "nested-child", "isComplete": True}})
                    + log_record({"msg": "Main prompt finished", "data": {"outputType": "success"}}),
                metadata={"runState": {"isComplete": True}, "isComplete": True},
            )

        def on_chunk(text, reasoning):
            emitted.append((text, reasoning))
            seen_partial.set()

        self.on_submit(submit)
        task = asyncio.create_task(self.execute(on_chunk=on_chunk))
        try:
            await asyncio.wait_for(seen_partial.wait(), 0.5)
            await asyncio.sleep(0.18)
            self.assertFalse(task.done(), "Internal completion markers ended a partial public response")
            self.write_messages(state["chat"], [user(), answer(text="partial final", complete=True)])
            result = await task
            self.assertEqual(result["content"], "partial final")
            self.assertEqual(emitted, [("partial", False), (" final", False)])
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_switching_directories_after_output_is_bound_is_rejected(self):
        emitted = asyncio.Event()
        self.on_submit(lambda _command: self.create_chat([user(), answer(text="part", complete=False)]))
        task = asyncio.create_task(self.execute(on_chunk=lambda _text, _reasoning: emitted.set()))
        try:
            await asyncio.wait_for(emitted.wait(), 0.5)
            self.create_chat([user("other-new-user"), answer("ai-other-new", "part from another chat")])
            with self.assertRaises(FreebuffServerError):
                await task
            self.worker.stop.assert_awaited_once()
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_only_public_top_level_blocks_are_streamed_in_their_channels(self):
        ai = answer()
        ai["blocks"] = [
            {"type": "text", "textType": "reasoning", "content": "public reasoning"},
            {"type": "agent", "blocks": [{"type": "text", "content": "private nested trace"}]},
            {"type": "text", "content": "public answer"},
        ]
        ai["content"] = "legacy fallback must not be read"
        self.on_submit(lambda _command: self.create_chat([user(), ai]))
        emitted = []
        result = await self.execute(on_chunk=lambda text, reasoning: emitted.append((text, reasoning)))
        self.assertEqual(result["content"], "public answer")
        self.assertEqual(result["reasoning_content"], "public reasoning")
        self.assertEqual(emitted, [("public reasoning", True), ("public answer", False)])

    async def test_transcript_user_error_prevents_success(self):
        self.on_submit(lambda _command: self.create_chat([user(), answer(userError="private upstream debug details")]))
        with self.assertRaises(FreebuffServerError) as caught:
            await self.execute()
        self.assertNotIn("private upstream", str(caught.exception))
        self.worker.stop.assert_awaited_once()

    async def test_cleanup_failure_does_not_replace_typed_upstream_error(self):
        self.on_submit(lambda _command: self.create_chat([], logs=log_record({"data": {"status": 401}})))
        self.worker.stop.side_effect = asyncio.TimeoutError
        with self.assertRaises(FreebuffAuthError):
            await self.execute()
        self.worker.stop.assert_awaited_once()
        self.assertFalse(self.worker.turn_lock.locked())

    async def test_new_log_http_errors_are_preserved_and_not_swallowed(self):
        types = {400: FreebuffBadRequestError, 401: FreebuffAuthError, 402: FreebuffPaymentError,
                 403: FreebuffPermissionError, 429: FreebuffRateLimitError, 503: FreebuffServerError}
        for status, error_type in types.items():
            with self.subTest(status=status):
                self.worker.is_running = True
                self.worker.master_fd = -1
                self.on_submit(lambda _command: self.create_chat([], logs=log_record({
                    "msg": "request failed with private-token", "data": {"status": status, "retryAfter": 7},
                })))
                with self.assertRaises(error_type) as caught:
                    await self.execute()
                self.assertEqual(caught.exception.status_code, status)
                self.assertNotIn("private-token", str(caught.exception))
                if status == 429:
                    self.assertEqual(caught.exception.retry_after, 7)

    async def test_new_error_in_previous_chat_while_reset_is_pending_keeps_typed_status(self):
        def submit(_command):
            with (self.old / "log.jsonl").open("ab") as stream:
                stream.write(log_record({"data": {"status": 403}, "msg": "request denied"}))
        self.on_submit(submit)
        with self.assertRaises(FreebuffPermissionError):
            await self.execute(timeout=0.08)

    async def test_errors_before_submission_are_not_replayed(self):
        (self.old / "log.jsonl").write_bytes(log_record({"data": {"status": 401}}))
        self.on_submit(lambda _command: self.write_messages(self.old, [user(), answer()]))
        result = await self.execute(reset=False)
        self.assertEqual(result["content"], "fresh answer")

    async def test_partial_utf8_log_error_waits_for_complete_record_then_raises(self):
        state = {}
        first_scan = asyncio.Event()
        event = log_record({"msg": "服务器拒绝", "data": {"status": 429, "retryAfter": 2}})
        split_at = event.index("服".encode("utf-8")) + 1

        def submit(_command):
            state["chat"] = self.create_chat([user(), answer(text="partial", complete=False)], logs=event[:split_at])

        self.on_submit(submit)
        task = asyncio.create_task(self.execute(on_chunk=lambda _text, _reasoning: first_scan.set()))
        try:
            await asyncio.wait_for(first_scan.wait(), 0.5)
            self.assertFalse(task.done())
            with (state["chat"] / "log.jsonl").open("ab") as stream:
                stream.write(event[split_at:])
            with self.assertRaises(FreebuffRateLimitError) as caught:
                await task
            self.assertEqual(caught.exception.retry_after, 2)
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_ask_user_public_block_reports_interaction_required(self):
        ai = answer()
        ai["blocks"] = [{"type": "ask_user", "answered": False, "question": "Choose a project"}]
        self.on_submit(lambda _command: self.create_chat([user(), ai]))
        with self.assertRaises(FreebuffInteractionRequiredError) as caught:
            await self.execute()
        self.assertEqual(caught.exception.status_code, 409)
        self.worker.stop.assert_awaited_once()

    async def test_ask_user_log_reports_interaction_required(self):
        self.on_submit(lambda _command: self.create_chat([], logs=log_record({"data": {"toolName": "ask_user"}})))
        with self.assertRaises(FreebuffInteractionRequiredError):
            await self.execute()

    async def test_takeover_dialog_is_not_automatically_confirmed(self):
        self.worker._raw_buffer.extend(b"This session runs elsewhere. Take over?")
        with patch("app.worker.os.write") as terminal_write:
            with self.assertRaises(FreebuffInteractionRequiredError):
                await self.execute()
        terminal_write.assert_not_called()
        self.worker.jitter_simulator.paste_into_fd.assert_not_awaited()

    async def test_cancellation_stops_partial_turn_and_releases_turn_lock(self):
        emitted = asyncio.Event()
        self.on_submit(lambda _command: self.create_chat([user(), answer(complete=False)]))
        task = asyncio.create_task(self.execute(on_chunk=lambda _text, _reasoning: emitted.set()))
        await asyncio.wait_for(emitted.wait(), 0.5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.worker.stop.assert_awaited_once()
        self.assertFalse(self.worker.turn_lock.locked())

    async def test_repeated_cancellation_keeps_lock_until_cleanup_finishes(self):
        emitted, stopping, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        self.on_submit(lambda _command: self.create_chat([user(), answer(complete=False)]))
        async def stop_slowly():
            stopping.set()
            await release.wait()
            self.worker.is_running = False
        self.worker.stop.side_effect = stop_slowly
        task = asyncio.create_task(self.execute(on_chunk=lambda _text, _reasoning: emitted.set()))
        try:
            await asyncio.wait_for(emitted.wait(), 0.5)
            task.cancel()
            await asyncio.wait_for(stopping.wait(), 0.5)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            self.assertTrue(self.worker.turn_lock.locked())
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertFalse(self.worker.turn_lock.locked())
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    async def test_replacement_answer_with_matching_prefix_is_not_spliced(self):
        emitted, state = asyncio.Event(), {}
        def submit(_command):
            state["chat"] = self.create_chat([user(), answer(text="part", complete=False)])
        self.on_submit(submit)
        task = asyncio.create_task(self.execute(on_chunk=lambda _text, _reasoning: emitted.set()))
        try:
            await asyncio.wait_for(emitted.wait(), 0.5)
            self.write_messages(state["chat"], [user(), answer("ai-replacement", "part from another run")])
            with self.assertRaisesRegex(FreebuffServerError, "identities"):
                await task
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_long_paste_matches_each_cli_transcript_format(self):
        for length in (2000, 2001, 32 * 1024, 128 * 1024):
            for representation in ("plain", "prefixed", "attachment"):
                for reset in (True, False):
                    with self.subTest(length=length, representation=representation, reset=reset):
                        prompt = ("你好😀\n" * (length // 4 + 1))[:length].rstrip()
                        def submit(command):
                            self.assertEqual(command, "[Current Request]\n" + prompt)
                            message = user(f"user-{self.sequence + 1}", prompt=command)
                            if representation == "prefixed":
                                message["content"] = "[Pasted Text]\n" + command
                            elif representation == "attachment":
                                message["content"] = ""
                                message["textAttachments"] = [{"id": "paste-1", "content": command}]
                            self.create_chat([message, answer(f"ai-{self.sequence + 1}")])
                        self.on_submit(submit)
                        emitted = []
                        result = await self.execute(prompt, reset=reset, on_chunk=lambda *chunk: emitted.append(chunk))
                        self.assertEqual(result["content"], "fresh answer")
                        self.assertEqual(emitted, [("fresh answer", False)])
        self.worker.stop.assert_not_awaited()

    async def test_upstream_cancelled_completion_cannot_be_success(self):
        self.on_submit(lambda _command: self.create_chat([
            user(), answer(text="partial\n\n[response interrupted]", metadata={}),
        ]))
        emitted = []
        with self.assertRaises(FreebuffServerError):
            await self.execute(on_chunk=lambda *chunk: emitted.append(chunk))
        self.assertEqual(emitted, [])
        self.worker.stop.assert_awaited_once()


class PublicTranscriptValidationTests(unittest.TestCase):
    def test_dividers_and_command_notices_are_not_root_responses(self):
        ai = answer()
        notice = answer("notice-1", "This is a system notice")
        self.assertEqual(current_response([user(), ai, notice], set(), WIRE_HELLO)["id"], "ai-new")

    def test_multiple_fresh_matching_users_are_ambiguous(self):
        with self.assertRaises(FreebuffServerError):
            current_response([user("user-one"), answer("ai-one"), user("user-two"), answer("ai-two")], set(), WIRE_HELLO)

    def test_multiple_root_responses_are_ambiguous(self):
        with self.assertRaises(FreebuffServerError):
            current_response([user(), answer("ai-one"), answer("ai-two")], set(), WIRE_HELLO)

    def test_incomplete_successful_run_metadata_does_not_complete_public_answer(self):
        ai = answer(complete=True)
        ai["isComplete"] = False
        self.assertFalse(current_response([user(), ai], set(), WIRE_HELLO)["complete"])

    def test_error_or_missing_run_output_never_completes_public_answer(self):
        for metadata in ({}, {"runState": {}}, {"runState": {"output": {"type": "error"}}}):
            with self.subTest(metadata=metadata):
                with self.assertRaises(FreebuffServerError):
                    current_response([user(), answer(metadata=metadata)], set(), WIRE_HELLO)

    def test_actual_sdk_error_statuscode_remains_typed(self):
        with self.assertRaises(FreebuffRateLimitError):
            check_log_error({"msg": "SDK client.run() failed", "data": {"error": {
                "name": "APICallError", "statusCode": 429, "message": "private error body",
            }}})

    def test_prompt_validation_has_no_paste_threshold_cap(self):
        for prompt in ("x" * 1995, "x" * 2001, "😀" * 1001, "😀" * (128 * 1024)):
            for reset in (True, False):
                with self.subTest(length=len(prompt), reset=reset):
                    self.assertEqual(validate_turn_prompt(prompt, reset), prompt)

    def test_long_prompt_still_rejects_invalid_unicode_and_terminal_controls(self):
        for suffix in ("\ud800", "\x1b[201~", "\x00", "\x7f", "\x85"):
            with self.subTest(suffix=repr(suffix)):
                with self.assertRaises(FreebuffBadRequestError):
                    validate_turn_prompt("x" * (128 * 1024) + suffix + "tail")

    def test_public_ai_root_content_fallback_still_requires_current_user_identity(self):
        ai = answer()
        ai["blocks"] = [{"type": "text", "textType": "reasoning", "content": "public reasoning"}]
        ai["content"] = "public root answer"
        ai["metadata"] = {"runState": {"output": {"type": "allMessages", "value": "private internal content"}}}
        response = current_response([user(), ai], set(), WIRE_HELLO)
        self.assertEqual(response["content"], "")
        self.assertEqual(response["reasoning_content"], "public reasoning")
        ai["blocks"] = []
        response = current_response([user(), ai], set(), WIRE_HELLO)
        self.assertEqual(response["content"], "public root answer")
        self.assertEqual(response["reasoning_content"], "")
        self.assertIsNone(current_response([user(), ai], {"user-new"}, WIRE_HELLO))

    def test_ai_message_without_id_cannot_be_accepted_as_completed_response(self):
        ai = answer()
        ai.pop("id")
        with self.assertRaises(FreebuffServerError):
            current_response([user(), ai], set(), WIRE_HELLO)

    def test_parented_ai_messages_are_never_used_as_root_answers(self):
        for parent_id in ("different-user", "user-new", "ai-parent"):
            with self.subTest(parent_id=parent_id):
                self.assertIsNone(current_response([user(), answer(parentId=parent_id)], set(), WIRE_HELLO))

    def test_ask_user_completion_requires_actual_answers_or_skip_marker(self):
        ai = answer()
        block = {"type": "ask-user", "answered": True}
        ai["blocks"] = [block, {"type": "text", "content": "ready"}]
        with self.assertRaises(FreebuffInteractionRequiredError):
            current_response([user(), ai], set(), WIRE_HELLO)
        for resolved in ({"answers": {"project": "one"}}, {"skipped": True}):
            with self.subTest(resolved=resolved):
                ai["blocks"][0] = {"type": "ask-user", **resolved}
                self.assertEqual(current_response([user(), ai], set(), WIRE_HELLO)["content"], "ready")

    def test_partial_multibyte_transcript_can_be_retried_after_file_finishes(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "chat-messages.json"
            messages = [user(), answer(text="你好")]
            encoded = json.dumps(messages, ensure_ascii=False).encode("utf-8")
            split_at = encoded.index("你".encode("utf-8")) + 1
            path.write_bytes(encoded[:split_at])
            self.assertEqual(read_messages(temp), [])
            with path.open("ab") as stream:
                stream.write(encoded[split_at:])
            self.assertEqual(read_messages(temp), messages)


class WorkerStopFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_stop_calls_share_one_cleanup(self):
        with tempfile.TemporaryDirectory() as temp:
            worker = FreebuffWorker(workdir=temp, home_dir=str(Path(temp) / "account"))
            entered, release = asyncio.Event(), asyncio.Event()
            supervisor = SimpleNamespace(returncode=None)
            async def wait():
                entered.set()
                await release.wait()
                supervisor.returncode = 0
            supervisor.wait = AsyncMock(side_effect=wait)
            worker._supervisor = supervisor
            first = asyncio.create_task(worker.stop())
            await entered.wait()
            second = asyncio.create_task(worker.stop())
            await asyncio.sleep(0)
            release.set()
            await asyncio.gather(first, second)
            supervisor.wait.assert_awaited_once()
            self.assertIsNone(worker._supervisor)

    async def test_supervisor_wait_failure_still_closes_terminal_and_lifetime_fds(self):
        with tempfile.TemporaryDirectory() as temp:
            worker = FreebuffWorker(workdir=temp, home_dir=str(Path(temp) / "account"))
            terminal_read, terminal_write = os.pipe()
            lifetime_read, lifetime_write = os.pipe()
            worker.master_fd = terminal_read
            worker._lifetime_fd = lifetime_write
            worker._loop = asyncio.get_running_loop()
            worker.is_running = True
            worker.pid = 12345
            supervisor = SimpleNamespace(wait=AsyncMock(side_effect=asyncio.TimeoutError), returncode=None)
            worker._supervisor = supervisor
            try:
                with self.assertRaises(asyncio.TimeoutError):
                    await worker.stop()
                self.assertFalse(worker.is_running)
                self.assertIsNone(worker.master_fd)
                self.assertIsNone(worker._lifetime_fd)
                for descriptor in (terminal_read, lifetime_write):
                    with self.assertRaises(OSError):
                        os.fstat(descriptor)
                self.assertIs(worker._supervisor, supervisor)
            finally:
                for descriptor in (terminal_read, terminal_write, lifetime_read, lifetime_write):
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass


if __name__ == "__main__":
    unittest.main()
