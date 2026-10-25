"""Offline ASGI regressions. No lifespan startup, CLI process, or network access."""
import asyncio
import json
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import main
from app.exceptions import FreebuffException
from app.models import ChatCompletionRequest, PricingResponse
from app.routes import chat


class ASGIExchange:
    """Drive the actual app with ASGI messages, including real disconnect events."""
    def __init__(self, payload=None, *, method="POST", path="/v1/chat/completions", headers=None, spec="2.3", send_hook=None):
        self.sent = []
        self.incoming = asyncio.Queue()
        self.incoming.put_nowait({
            "type": "http.request",
            "body": json.dumps(payload).encode() if payload is not None else b"",
            "more_body": False,
        })
        self.scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": spec},
            "http_version": "1.1", "method": method, "scheme": "http",
            "path": path, "raw_path": path.encode(), "query_string": b"",
            "root_path": "", "client": ("127.0.0.1", 12000), "server": ("test", 80),
            "headers": [(b"content-type", b"application/json")] + [
                (key.lower().encode(), value.encode()) for key, value in (headers or {}).items()
            ],
        }
        self.send_hook = send_hook

    async def send(self, message):
        self.sent.append(message)
        if self.send_hook is not None:
            await self.send_hook(message)

    async def run(self):
        await main.app(self.scope, self.incoming.get, self.send)
        return self

    def disconnect(self):
        self.incoming.put_nowait({"type": "http.disconnect"})

    @property
    def status(self):
        return next((item["status"] for item in self.sent if item["type"] == "http.response.start"), None)

    @property
    def headers(self):
        starts = [item for item in self.sent if item["type"] == "http.response.start"]
        return dict(starts[0]["headers"]) if starts else {}

    @property
    def body(self):
        return b"".join(item.get("body", b"") for item in self.sent if item["type"] == "http.response.body")

    @property
    def json(self):
        return json.loads(self.body)

    @property
    def events(self):
        return [
            json.loads(line[6:]) if line[6:] != "[DONE]" else "[DONE]"
            for line in self.body.decode().splitlines() if line.startswith("data: ")
        ]

    def deltas(self, channel):
        return [
            event["choices"][0]["delta"][channel]
            for event in self.events if isinstance(event, dict) and "choices" in event
            and channel in event["choices"][0]["delta"]
        ]


class ChatProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.auth = patch.object(main, "API_KEY", "")
        self.catalog = patch.object(chat, "resolve_model_id", return_value="test-model")
        self.workspace = patch.object(chat, "resolve_target_workspace", return_value="/tmp/offline-chat-test")
        self.executor = patch.object(chat.global_pool, "execute_chat", new_callable=AsyncMock)
        self.auth.start()
        self.resolve = self.catalog.start()
        self.resolve_workspace = self.workspace.start()
        self.execute = self.executor.start()
        self.execute.return_value = {"content": "Hello"}
        self.addCleanup(self.auth.stop)
        self.addCleanup(self.catalog.stop)
        self.addCleanup(self.workspace.stop)
        self.addCleanup(self.executor.stop)

    async def request(self, *, stream=False, **kwargs):
        payload = {"model": "test-model", "messages": [{"role": "user", "content": "Hi"}], "stream": stream}
        payload.update(kwargs)
        exchange = ASGIExchange(payload)
        return await asyncio.wait_for(exchange.run(), timeout=2)

    def assert_no_chat_tasks(self):
        leaked = [task.get_name() for task in asyncio.all_tasks() if task.get_name().startswith("chat-") and not task.done()]
        self.assertEqual(leaked, [])

    async def test_both_stream_channels_are_emitted_exactly_once(self):
        async def execute(**kwargs):
            kwargs["on_chunk"]("Think", True)
            kwargs["on_chunk"]("Hello", False)
            kwargs["on_chunk"](" world", False)
            return {"content": "Hello world", "reasoning_content": "Think"}
        self.execute.side_effect = execute
        response = await self.request(stream=True)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.deltas("content"), ["Hello", " world"])
        self.assertEqual(response.deltas("reasoning_content"), ["Think"])
        self.assertEqual(response.events[-1], "[DONE]")
        self.assertEqual(response.events[-2]["choices"][0]["finish_reason"], "stop")
        self.assert_no_chat_tasks()

    async def test_final_partial_suffixes_are_reconciled_independently(self):
        async def execute(**kwargs):
            kwargs["on_chunk"]("Hello", False)
            kwargs["on_chunk"]("Think", True)
            return {"content": "Hello world", "reasoning_content": "Thinking"}
        self.execute.side_effect = execute
        response = await self.request(stream=True)
        self.assertEqual(response.deltas("content"), ["Hello", " world"])
        self.assertEqual(response.deltas("reasoning_content"), ["Think", "ing"])

    async def test_final_only_result_streams_once(self):
        self.execute.return_value = {"content": "Final text", "reasoning_content": "Final reasoning"}
        response = await self.request(stream=True)
        self.assertEqual(response.deltas("content"), ["Final text"])
        self.assertEqual(response.deltas("reasoning_content"), ["Final reasoning"])

    async def test_changed_final_text_fails_without_concatenating_answers(self):
        for mismatch_channel in ("content", "reasoning_content"):
            with self.subTest(channel=mismatch_channel):
                async def execute(**kwargs):
                    kwargs["on_chunk"]("prefix", mismatch_channel == "reasoning_content")
                    return {"content": "different", "reasoning_content": "different"}
                self.execute.side_effect = execute
                response = await self.request(stream=True)
                self.assertEqual(response.deltas(mismatch_channel), ["prefix"])
                self.assertEqual(response.deltas("content" if mismatch_channel == "reasoning_content" else "reasoning_content"), [])
                self.assertEqual(response.events[-2]["error"]["code"], "stream_mismatch")
                self.assertFalse(any(isinstance(e, dict) and e.get("choices", [{}])[0].get("finish_reason") == "stop" for e in response.events))

    async def test_early_errors_keep_http_status_and_openai_shape(self):
        for stream in (True, False):
            for status in (401, 403, 429, 502, 504):
                with self.subTest(stream=stream, status=status):
                    error = FreebuffException("offline failure", status_code=status, error_type="test_error", error_code="test_code")
                    error.retry_after = 12 if status == 429 else None
                    self.execute.side_effect = error
                    response = await self.request(stream=stream)
                    self.assertEqual(response.status, status)
                    self.assertNotIn("detail", response.json)
                    self.assertEqual(response.json["error"]["type"], "test_error")
                    self.assertEqual(response.json["error"]["code"], "test_code")
                    if status == 429:
                        self.assertEqual(response.headers[b"retry-after"], b"12")
                    self.assertNotIn(b"text/event-stream", response.headers[b"content-type"])
        self.assert_no_chat_tasks()

    async def test_headers_wait_for_first_data_or_result(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def execute(**kwargs):
            started.set()
            await release.wait()
            return {"content": "Ready"}
        self.execute.side_effect = execute
        exchange = ASGIExchange({"stream": True, "messages": [{"role": "user", "content": "Hi"}]})
        task = asyncio.create_task(exchange.run())
        try:
            await asyncio.wait_for(started.wait(), 2)
            self.assertIsNone(exchange.status)
            release.set()
            await asyncio.wait_for(task, 2)
            self.assertEqual(exchange.status, 200)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_post_data_failure_is_terminal_sse_error(self):
        async def execute(**kwargs):
            kwargs["on_chunk"]("Partial", False)
            raise FreebuffException("failed after output", 429, error_code="rate_limit_exceeded")
        self.execute.side_effect = execute
        response = await self.request(stream=True)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.deltas("content"), ["Partial"])
        self.assertEqual(response.events[-2]["error"]["code"], "rate_limit_exceeded")
        self.assertEqual(response.events[-1], "[DONE]")

    async def test_disconnect_before_headers_cancels_and_awaits_execution(self):
        for stream in (True, False):
            with self.subTest(stream=stream):
                started, cleaned = asyncio.Event(), asyncio.Event()
                async def execute(**kwargs):
                    started.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        await asyncio.sleep(0)
                        cleaned.set()
                self.execute.side_effect = execute
                exchange = ASGIExchange({"stream": stream, "messages": [{"role": "user", "content": "Hi"}]})
                task = asyncio.create_task(exchange.run())
                await asyncio.wait_for(started.wait(), 2)
                exchange.disconnect()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 2)
                self.assertTrue(cleaned.is_set())
                self.assertIsNone(exchange.status)
                self.assert_no_chat_tasks()

    async def test_request_task_cancellation_before_headers_cleans_execution(self):
        started, cleaned = asyncio.Event(), asyncio.Event()
        async def execute(**kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                cleaned.set()
        self.execute.side_effect = execute
        exchange = ASGIExchange({"stream": True, "messages": [{"role": "user", "content": "Hi"}]})
        task = asyncio.create_task(exchange.run())
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        self.assertTrue(cleaned.is_set())
        self.assert_no_chat_tasks()

    async def test_request_task_cancellation_during_stream_cleans_execution(self):
        emitted, cleaned = asyncio.Event(), asyncio.Event()
        async def execute(**kwargs):
            kwargs["on_chunk"]("Partial", False)
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                cleaned.set()
        self.execute.side_effect = execute
        async def on_send(message):
            if b"Partial" in message.get("body", b""):
                emitted.set()
        exchange = ASGIExchange({"stream": True, "messages": [{"role": "user", "content": "Hi"}]}, send_hook=on_send)
        task = asyncio.create_task(exchange.run())
        await asyncio.wait_for(emitted.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        self.assertTrue(cleaned.is_set())
        self.assert_no_chat_tasks()

    async def test_repeated_request_cancellation_does_not_interrupt_cleanup(self):
        for during_stream in (False, True):
            with self.subTest(during_stream=during_stream):
                started, cleaning, release, cleaned = (asyncio.Event() for _ in range(4))

                async def execute(**kwargs):
                    if during_stream:
                        kwargs["on_chunk"]("Partial", False)
                    else:
                        started.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        cleaning.set()
                        await release.wait()
                        cleaned.set()

                async def on_send(message):
                    if b"Partial" in message.get("body", b""):
                        started.set()

                self.execute.side_effect = execute
                exchange = ASGIExchange(
                    {"stream": True, "messages": [{"role": "user", "content": "Hi"}]},
                    send_hook=on_send,
                )
                task = asyncio.create_task(exchange.run())
                try:
                    await asyncio.wait_for(started.wait(), 2)
                    task.cancel()
                    await asyncio.wait_for(cleaning.wait(), 2)
                    task.cancel()
                    await asyncio.sleep(0)
                    self.assertFalse(task.done())
                finally:
                    release.set()
                    await asyncio.gather(task, return_exceptions=True)
                self.assertTrue(cleaned.is_set())
                self.assertTrue(task.cancelled())
                self.assert_no_chat_tasks()

    async def test_disconnect_after_first_chunk_stops_background_execution(self):
        for spec in ("2.3", "2.4"):
            with self.subTest(spec=spec):
                cleaned = asyncio.Event()
                async def execute(**kwargs):
                    kwargs["on_chunk"]("Partial", False)
                    try:
                        await asyncio.Event().wait()
                    finally:
                        await asyncio.sleep(0)
                        cleaned.set()
                self.execute.side_effect = execute
                exchange = ASGIExchange({"stream": True, "messages": [{"role": "user", "content": "Hi"}]}, spec=spec)
                async def on_send(message):
                    if b"Partial" in message.get("body", b""):
                        exchange.disconnect()
                exchange.send_hook = on_send
                await asyncio.wait_for(exchange.run(), 2)
                self.assertTrue(cleaned.is_set())
                self.assertEqual(exchange.deltas("content"), ["Partial"])
                self.assert_no_chat_tasks()

    async def test_send_failure_before_body_closes_execution(self):
        cleaned = asyncio.Event()
        async def execute(**kwargs):
            kwargs["on_chunk"]("Partial", False)
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                cleaned.set()
        self.execute.side_effect = execute
        async def on_send(message):
            if message["type"] == "http.response.start":
                raise OSError("client left")
        exchange = ASGIExchange({"stream": True, "messages": [{"role": "user", "content": "Hi"}]}, spec="2.4", send_hook=on_send)
        from starlette.requests import ClientDisconnect
        with self.assertRaises(ClientDisconnect):
            await asyncio.wait_for(exchange.run(), 2)
        self.assertTrue(cleaned.is_set())
        self.assert_no_chat_tasks()

    async def test_text_arrays_are_normalized_before_pool_execution(self):
        response = await self.request(messages=[{"role": "user", "content": [
            {"type": "text", "text": "Hello "}, {"type": "text", "text": "world"},
        ]}])
        self.assertEqual(response.status, 200)
        self.assertEqual(self.execute.await_args.kwargs["messages"][0].content, "Hello world")
        self.assertEqual(response.json["choices"][0]["message"]["content"], "Hello")

    async def test_unavailable_token_usage_is_not_estimated(self):
        response = await self.request()
        self.assertIsNone(response.json["usage"])

    async def test_explicit_empty_workspace_reaches_boundary_validation(self):
        self.resolve_workspace.side_effect = FreebuffException(
            "Workspace must not be empty.", status_code=400,
            error_type="invalid_request_error", error_code="invalid_workspace",
        )
        response = await self.request(workspace="", cwd="/otherwise-valid")
        self.assertEqual(response.status, 400)
        self.assertEqual(self.resolve_workspace.call_args.kwargs["body_workspace"], "")
        self.execute.assert_not_awaited()

    async def test_malformed_worker_result_fails_before_headers(self):
        for result in ([], {}, {"content": None}, {"content": 0}, {"reasoning_content": []}):
            with self.subTest(result=result):
                self.execute.return_value = result
                response = await self.request(stream=True)
                self.assertEqual(response.status, 502)
                self.assertEqual(response.json["error"]["code"], "invalid_upstream_response")

    async def test_missing_public_text_is_not_a_successful_completion(self):
        self.execute.return_value = {"content": "", "reasoning_content": ""}
        for stream in (False, True):
            with self.subTest(stream=stream):
                response = await self.request(stream=stream)
                self.assertEqual(response.status, 502)
                self.assertEqual(response.json["error"]["code"], "empty_upstream_response")

    async def test_malformed_delta_fails_before_headers(self):
        for text, channel in ((0, False), (None, False), ("text", "reasoning")):
            with self.subTest(text=text, channel=channel):
                async def execute(**kwargs):
                    kwargs["on_chunk"](text, channel)
                    return {"content": "Unused"}
                self.execute.side_effect = execute
                response = await self.request(stream=True)
                self.assertEqual(response.status, 502)
                self.assertEqual(response.json["error"]["code"], "invalid_upstream_response")

    async def test_assistant_prefill_and_empty_current_requests_are_rejected(self):
        samples = [
            [{"role": "assistant", "content": "A partial answer"}],
            [{"role": "system", "content": "A system-only request"}],
            [{"role": "user", "content": "   "}],
            [{"role": "user", "content": []}],
        ]
        for messages in samples:
            with self.subTest(messages=messages):
                response = await self.request(messages=messages)
                self.assertEqual(response.status, 422)
                self.assertIn("final message", response.json["error"]["message"])
        self.execute.assert_not_awaited()

    async def test_unsupported_messages_fail_explicitly_without_execution(self):
        samples = [
            {"role": "user", "content": None},
            {"role": "tool", "content": None, "tool_call_id": "call_a"},
            {"role": "assistant", "content": None, "tool_calls": []},
            {"role": "assistant", "content": "", "tool_calls": []},
            {"role": "function", "content": "result", "name": "fn"},
            {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:img"}}]},
            {"role": "user", "content": [{"type": "input_audio", "input_audio": {"data": "audio", "format": "wav"}}]},
            {"role": "user", "content": [{"type": "text", "text": "text", "extra": "silently dropped?"}]},
            {"role": "user", "content": "Hi", "name": "speaker"},
        ]
        for message in samples:
            with self.subTest(message=message):
                response = await self.request(messages=[message])
                self.assertEqual(response.status, 422)
                self.assertEqual(response.json["error"]["type"], "invalid_request_error")
                self.assertNotIn("detail", response.json)
        self.execute.assert_not_awaited()

    async def test_explicit_unsupported_parameters_do_not_silently_disappear(self):
        samples = {
            "temperature": 0.7, "max_tokens": 1, "max_completion_tokens": 1,
            "stop": ["end"], "top_p": 1, "presence_penalty": 0, "frequency_penalty": 0,
            "tools": [], "tool_choice": "auto", "functions": [], "response_format": {"type": "json_object"},
            "logprobs": False, "n": 1, "stream_options": {"include_usage": True},
            "metadata": {}, "seed": None,
        }
        for parameter, value in samples.items():
            with self.subTest(parameter=parameter):
                response = await self.request(**{parameter: value})
                self.assertEqual(response.status, 422)
                self.assertIn(parameter, response.json["error"]["message"])
        self.execute.assert_not_awaited()
        advertised = ChatCompletionRequest.model_json_schema()["properties"]
        self.assertTrue(set(samples).isdisjoint(advertised))

    async def test_unknown_model_has_no_default_fallback(self):
        with patch("app.models.catalog_service.resolve_model", return_value=None):
            from app.models import resolve_model_id
            self.resolve.side_effect = resolve_model_id
            response = await self.request(model="unknown-model")
        self.assertEqual(response.status, 404)
        self.assertEqual(response.json["error"]["code"], "model_not_found")
        self.execute.assert_not_awaited()

    async def test_catalog_resolution_runs_off_event_loop(self):
        event_loop_thread = threading.get_ident()
        catalog_threads = []
        def resolve(model):
            catalog_threads.append(threading.get_ident())
            return model
        self.resolve.side_effect = resolve
        response = await self.request()
        self.assertEqual(response.status, 200)
        self.assertNotEqual(catalog_threads, [event_loop_thread])

    async def test_cors_preflight_bypasses_auth_but_actual_request_requires_it(self):
        with patch.object(main, "API_KEY", "offline-key"):
            headers = {"Origin": "https://client.example", "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "Authorization,Content-Type"}
            preflight = await ASGIExchange(method="OPTIONS", headers=headers).run()
            self.assertEqual(preflight.status, 200)
            self.assertEqual(preflight.headers[b"access-control-allow-origin"], b"*")
            self.assertNotIn(b"access-control-allow-credentials", preflight.headers)
            rejected = await ASGIExchange({"messages": [{"role": "user", "content": "Hi"}]}, headers={"Origin": "https://client.example"}).run()
            self.assertEqual(rejected.status, 401)
            self.assertIn(b"access-control-allow-origin", rejected.headers)
            accepted = await ASGIExchange({"messages": [{"role": "user", "content": "Hi"}]}, headers={"Authorization": "Bearer offline-key"}).run()
            self.assertEqual(accepted.status, 200)

    async def test_request_body_limit_exceeded_by_content_length_returns_413(self):
        headers = {"content-length": str(main.MAX_REQUEST_BODY_BYTES + 1)}
        exchange = await ASGIExchange(method="POST", headers=headers).run()
        self.assertEqual(exchange.status, 413)
        self.assertEqual(exchange.json["error"]["code"], "request_too_large")

    async def test_request_body_limit_exceeded_by_chunked_stream_returns_413(self):
        exchange = ASGIExchange(method="POST")
        exchange.incoming.get_nowait()
        huge_chunk = b"x" * (main.MAX_REQUEST_BODY_BYTES + 10)
        exchange.incoming.put_nowait({"type": "http.request", "body": huge_chunk, "more_body": False})
        await exchange.run()
        self.assertEqual(exchange.status, 413)
        self.assertEqual(exchange.json["error"]["code"], "request_too_large")

    def test_unknown_pricing_values_remain_null(self):
        result = PricingResponse(models_count=0, models=[], status="unknown", source="fallback")
        self.assertIsNone(result.account)
        self.assertIsNone(result.balance)
        result = PricingResponse(models_count=0, models=[], account={}, balance={"daily": {}, "wallet": {}})
        self.assertIsNone(result.account.country_verified)
        self.assertIsNone(result.balance.daily.remaining_freebucks)
        self.assertIsNone(result.balance.wallet.monthly_bonus)


if __name__ == "__main__":
    unittest.main()
