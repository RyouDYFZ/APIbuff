import time
import uuid
import json
import asyncio
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from ..models import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatCompletionResponseChoice,
    ChatCompletionResponseMessage,
    ChatCompletionChunk,
    ChatCompletionChunkChoice,
    ChatCompletionChunkDelta,
    UsageInfo,
    resolve_model_id
)
from ..pool import global_pool
from ..workspace import resolve_target_workspace
from ..exceptions import FreebuffException

router = APIRouter()

@router.post("/v1/chat/completions")
@router.post("/chat/completions")
async def chat_completions(request: ChatCompletionRequest, http_request: Request):
    """
    OpenAI-compatible /v1/chat/completions endpoint.
    Supports both standard non-streaming responses and Server-Sent Events (SSE) streaming.
    Automatically resolves client workspace directory and supports stateful session continuity.
    Fails fast with typed OpenAI error payloads upon encountering auth, rate limit, or proxy blocks.
    """
    canonical_model = resolve_model_id(request.model)
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created_ts = int(time.time())

    # Resolve target project workspace
    target_workspace = resolve_target_workspace(
        http_request=http_request,
        body_workspace=request.workspace or request.cwd or request.project_dir,
        messages=request.messages
    )

    # Extract session ID from headers or body
    session_id = (
        request.session_id or
        http_request.headers.get("X-Session-ID") or
        http_request.headers.get("X-Conversation-ID")
    )

    if request.stream:
        async def stream_generator():
            try:
                # Initial chunk with role
                first_chunk = ChatCompletionChunk(
                    id=completion_id,
                    created=created_ts,
                    model=canonical_model,
                    choices=[
                        ChatCompletionChunkChoice(
                            index=0,
                            delta=ChatCompletionChunkDelta(role="assistant"),
                            finish_reason=None
                        )
                    ]
                )
                yield f"data: {first_chunk.model_dump_json()}\n\n"

                chunk_queue = asyncio.Queue()

                def chunk_callback(token_text: str, is_reasoning: bool):
                    chunk_queue.put_nowait((token_text, is_reasoning))

                # Run execution in a background task
                exec_task = asyncio.create_task(
                    global_pool.execute_chat(
                        messages=request.messages,
                        model=request.model,
                        workspace=target_workspace,
                        session_id=session_id,
                        reset_context=request.reset_context,
                        on_chunk=chunk_callback
                    )
                )

                # While task is running, yield any stream items from queue
                while not exec_task.done() or not chunk_queue.empty():
                    try:
                        token_text, is_reasoning = await asyncio.wait_for(chunk_queue.get(), timeout=0.1)
                        if is_reasoning:
                            delta = ChatCompletionChunkDelta(reasoning_content=token_text)
                        else:
                            delta = ChatCompletionChunkDelta(content=token_text)
                        
                        chunk = ChatCompletionChunk(
                            id=completion_id,
                            created=created_ts,
                            model=canonical_model,
                            choices=[ChatCompletionChunkChoice(index=0, delta=delta, finish_reason=None)]
                        )
                        yield f"data: {chunk.model_dump_json()}\n\n"
                    except asyncio.TimeoutError:
                        continue

                # Await the execution result (will raise FreebuffException if turn failed)
                result = await exec_task
                final_content = result.get("content", "")
                final_reasoning = result.get("reasoning_content")

                # If no streaming chunks were emitted during turn, emit the full response now
                if final_reasoning:
                    r_chunk = ChatCompletionChunk(
                        id=completion_id,
                        created=created_ts,
                        model=canonical_model,
                        choices=[
                            ChatCompletionChunkChoice(
                                index=0,
                                delta=ChatCompletionChunkDelta(reasoning_content=final_reasoning),
                                finish_reason=None
                            )
                        ]
                    )
                    yield f"data: {r_chunk.model_dump_json()}\n\n"

                if final_content:
                    c_chunk = ChatCompletionChunk(
                        id=completion_id,
                        created=created_ts,
                        model=canonical_model,
                        choices=[
                            ChatCompletionChunkChoice(
                                index=0,
                                delta=ChatCompletionChunkDelta(content=final_content),
                                finish_reason=None
                            )
                        ]
                    )
                    yield f"data: {c_chunk.model_dump_json()}\n\n"

                # Final finish_reason: "stop" chunk
                end_chunk = ChatCompletionChunk(
                    id=completion_id,
                    created=created_ts,
                    model=canonical_model,
                    choices=[
                        ChatCompletionChunkChoice(
                            index=0,
                            delta=ChatCompletionChunkDelta(),
                            finish_reason="stop"
                        )
                    ]
                )
                yield f"data: {end_chunk.model_dump_json()}\n\n"
                yield "data: [DONE]\n\n"

            except FreebuffException as e:
                err_payload = json.dumps(e.to_dict())
                yield f"data: {err_payload}\n\n"
                yield "data: [DONE]\n\n"
            except Exception as e:
                err_dict = {"error": {"message": str(e), "type": "api_error", "code": 500}}
                yield f"data: {json.dumps(err_dict)}\n\n"
                yield "data: [DONE]\n\n"

        return StreamingResponse(
            stream_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no"
            }
        )

    else:
        # Non-streaming standard response
        try:
            result = await global_pool.execute_chat(
                messages=request.messages,
                model=request.model,
                workspace=target_workspace,
                session_id=session_id,
                reset_context=request.reset_context
            )
        except FreebuffException as e:
            raise HTTPException(
                status_code=e.status_code,
                detail=e.to_dict()["error"]
            )

        content = result.get("content", "")
        reasoning = result.get("reasoning_content")

        response_message = ChatCompletionResponseMessage(
            role="assistant",
            content=content,
            reasoning_content=reasoning
        )

        prompt_len = sum(len(m.content) for m in request.messages)
        resp_len = len(content)

        return ChatCompletionResponse(
            id=completion_id,
            created=created_ts,
            model=canonical_model,
            choices=[
                ChatCompletionResponseChoice(
                    index=0,
                    message=response_message,
                    finish_reason="stop"
                )
            ],
            usage=UsageInfo(
                prompt_tokens=max(1, prompt_len // 4),
                completion_tokens=max(1, resp_len // 4),
                total_tokens=max(2, (prompt_len + resp_len) // 4)
            )
        )
