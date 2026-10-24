import asyncio
import json
import logging
import time
import uuid

import anyio
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.requests import ClientDisconnect

from ..models import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatCompletionResponseChoice,
    ChatCompletionResponseMessage,
    ChatCompletionChunk,
    ChatCompletionChunkChoice,
    ChatCompletionChunkDelta,
    resolve_model_id,
)
from ..pool import global_pool
from ..workspace import resolve_target_workspace
from ..exceptions import FreebuffException

router = APIRouter()
logger = logging.getLogger("freebuff.chat")


async def _cancel_and_wait(*tasks):
    """Return only after the execution (including worker cleanup) has stopped."""
    tasks = [task for task in tasks if task is not None]
    for task in tasks:
        cancelling = getattr(task, "cancelling", None)
        is_cancelling = cancelling() if cancelling is not None else task.cancelled()
        if not task.done() and not is_cancelling:
            task.cancel()
    # AnyIO scopes and repeated Task.cancel() calls must not cancel the cleanup
    # itself. Multiple owners may wait for the same execution during a disconnect.
    cancelled = None
    with anyio.CancelScope(shield=True):
        finished = asyncio.gather(*tasks, return_exceptions=True)
        while True:
            try:
                await asyncio.shield(finished)
                break
            except asyncio.CancelledError as error:
                cancelled = error
    if cancelled is not None:
        raise cancelled


async def _first_event(queue, http_request):
    """Wait before sending headers, observing disconnects during that wait."""
    async def wait_for_disconnect():
        while True:
            message = await http_request.receive()
            if message["type"] == "http.disconnect":
                return

    event_task = asyncio.create_task(queue.get(), name="chat-first-event")
    disconnect_task = asyncio.create_task(wait_for_disconnect(), name="chat-preflight-disconnect")
    try:
        done, _ = await asyncio.wait(
            (event_task, disconnect_task), return_when=asyncio.FIRST_COMPLETED
        )
        if disconnect_task in done:
            disconnect_task.result()
            raise asyncio.CancelledError()
        return event_task.result()
    finally:
        await _cancel_and_wait(event_task, disconnect_task)


class _ExecutionStreamingResponse(StreamingResponse):
    """Own the task even if sending headers/body fails before iteration starts."""
    def __init__(self, *args, execution_task, **kwargs):
        super().__init__(*args, **kwargs)
        self.execution_task = execution_task

    async def __call__(self, scope, receive, send):
        # Watch receive even on ASGI 2.4: a client can leave between tokens,
        # when there is no send() call available to report a disconnect.
        body_task = asyncio.create_task(self.stream_response(send), name="chat-stream-body")
        disconnect_task = asyncio.create_task(self.listen_for_disconnect(receive), name="chat-stream-disconnect")
        try:
            done, _ = await asyncio.wait(
                (body_task, disconnect_task), return_when=asyncio.FIRST_COMPLETED
            )
            if body_task in done:
                body_task.result()
            else:
                disconnect_task.result()
        except OSError as error:
            raise ClientDisconnect() from error
        finally:
            with anyio.CancelScope(shield=True):
                try:
                    await _cancel_and_wait(body_task, disconnect_task)
                    await self.body_iterator.aclose()
                finally:
                    await _cancel_and_wait(self.execution_task)
        if self.background is not None:
            await self.background()


def _error_response(error):
    headers = {}
    retry_after = getattr(error, "retry_after", None)
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return JSONResponse(status_code=error.status_code, content=error.to_dict(), headers=headers)


def _result_suffixes(result, emitted_content, emitted_reasoning):
    if not isinstance(result, dict):
        raise FreebuffException("Invalid completion result from the worker.", 502, error_code="invalid_upstream_response")
    content = result.get("content")
    reasoning = result.get("reasoning_content")
    reasoning = "" if reasoning is None else reasoning
    if not isinstance(content, str) or not isinstance(reasoning, str):
        raise FreebuffException("Invalid completion text from the worker.", 502, error_code="invalid_upstream_response")
    if not content and not reasoning:
        raise FreebuffException("The CLI completed without a public text response.", 502, error_code="empty_upstream_response")
    # Validate both channels before emitting either suffix. Changed final text
    # cannot safely be appended to bytes the client has already received.
    if not content.startswith(emitted_content) or not reasoning.startswith(emitted_reasoning):
        raise FreebuffException(
            "The final completion did not match the streamed text.",
            status_code=502,
            error_type="api_error",
            error_code="stream_mismatch",
        )
    return content[len(emitted_content):], reasoning[len(emitted_reasoning):]


@router.post("/v1/chat/completions")
@router.post("/chat/completions")
async def chat_completions(request: ChatCompletionRequest, http_request: Request):
    """OpenAI text completions with independently reconciled SSE channels."""
    execution_task = None
    streaming_owns_task = False
    try:
        canonical_model = await asyncio.to_thread(resolve_model_id, request.model)
        completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        created_ts = int(time.time())
        body_workspace = next(
            (value for value in (request.workspace, request.cwd, request.project_dir) if value is not None),
            None,
        )
        target_workspace = resolve_target_workspace(
            http_request=http_request,
            body_workspace=body_workspace,
            messages=request.messages,
        )
        session_id = (
            request.session_id
            or http_request.headers.get("X-Session-ID")
            or http_request.headers.get("X-Conversation-ID")
        )
        events = asyncio.Queue()

        def on_chunk(text, is_reasoning):
            if not isinstance(text, str) or not isinstance(is_reasoning, bool):
                raise FreebuffException(
                    "Invalid completion delta from the worker.", 502,
                    error_code="invalid_upstream_response",
                )
            if text:
                events.put_nowait(("chunk", (text, is_reasoning)))

        async def execute():
            try:
                result = await global_pool.execute_chat(
                    messages=request.messages,
                    model=canonical_model,
                    workspace=target_workspace,
                    session_id=session_id,
                    reset_context=request.reset_context,
                    on_chunk=on_chunk if request.stream else None,
                )
            except asyncio.CancelledError as error:
                events.put_nowait(("error", error))
                raise
            except Exception as error:
                events.put_nowait(("error", error))
            else:
                events.put_nowait(("result", result))

        execution_task = asyncio.create_task(execute(), name="chat-execution")
        first_event = await _first_event(events, http_request)
        if first_event[0] == "error":
            raise first_event[1]
        if first_event[0] == "result":
            # Malformed results must also fail before headers if no bytes streamed.
            _result_suffixes(first_event[1], "", "")

        if not request.stream:
            content, reasoning = _result_suffixes(first_event[1], "", "")
            return ChatCompletionResponse(
                id=completion_id,
                created=created_ts,
                model=canonical_model,
                choices=[ChatCompletionResponseChoice(message=ChatCompletionResponseMessage(
                    content=content, reasoning_content=reasoning or None,
                ))],
            )

        def chunk(delta, finish_reason=None):
            payload = ChatCompletionChunk(
                id=completion_id,
                created=created_ts,
                model=canonical_model,
                choices=[ChatCompletionChunkChoice(delta=delta, finish_reason=finish_reason)],
            )
            data = payload.model_dump()
            data["choices"][0]["delta"] = delta.model_dump(exclude_none=True)
            return f"data: {json.dumps(data)}\n\n"

        async def stream_generator():
            emitted_content = ""
            emitted_reasoning = ""
            event = first_event
            try:
                yield chunk(ChatCompletionChunkDelta(role="assistant"))
                while True:
                    kind, value = event
                    if kind == "error":
                        raise value
                    if kind == "chunk":
                        text, is_reasoning = value
                        if is_reasoning:
                            emitted_reasoning += text
                            delta = ChatCompletionChunkDelta(reasoning_content=text)
                        else:
                            emitted_content += text
                            delta = ChatCompletionChunkDelta(content=text)
                        yield chunk(delta)
                    else:
                        content_suffix, reasoning_suffix = _result_suffixes(
                            value, emitted_content, emitted_reasoning
                        )
                        if reasoning_suffix:
                            yield chunk(ChatCompletionChunkDelta(reasoning_content=reasoning_suffix))
                        if content_suffix:
                            yield chunk(ChatCompletionChunkDelta(content=content_suffix))
                        yield chunk(ChatCompletionChunkDelta(), finish_reason="stop")
                        yield "data: [DONE]\n\n"
                        return
                    event = await events.get()
            except FreebuffException as error:
                yield f"data: {json.dumps(error.to_dict())}\n\n"
                yield "data: [DONE]\n\n"
            except Exception:
                logger.exception("Completion stream failed")
                error = FreebuffException("Chat completion failed.", error_code="internal_error")
                yield f"data: {json.dumps(error.to_dict())}\n\n"
                yield "data: [DONE]\n\n"
            finally:
                await _cancel_and_wait(execution_task)

        response = _ExecutionStreamingResponse(
            stream_generator(),
            execution_task=execution_task,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
        streaming_owns_task = True
        return response
    except FreebuffException as error:
        return _error_response(error)
    except Exception:
        logger.exception("Completion request failed")
        return _error_response(FreebuffException("Chat completion failed.", error_code="internal_error"))
    finally:
        if not streaming_owns_task:
            await _cancel_and_wait(execution_task)
