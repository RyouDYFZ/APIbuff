import asyncio
import logging
from typing import List, Optional, Dict, Any, Callable
from .worker import FreebuffWorker
from .models import ChatMessage
from .catalog import catalog_service

logger = logging.getLogger("freebuff.pool")

class WorkerPool:
    def __init__(self):
        self.worker: Optional[FreebuffWorker] = None
        self._lock = asyncio.Lock()
        self.active_session_id: Optional[str] = None

    async def initialize(self):
        """Initializes the warm Freebuff CLI worker."""
        async with self._lock:
            if self.worker is None:
                self.worker = FreebuffWorker()
                await self.worker.start()

    async def shutdown(self):
        """Shuts down all workers cleanly."""
        async with self._lock:
            if self.worker is not None:
                await self.worker.stop()
                self.worker = None

    def format_messages(self, messages: List[ChatMessage], is_new_session: bool = True) -> str:
        """
        Formats OpenAI chat completion messages array into a prompt suitable for Freebuff.
        Handles system instructions, multi-turn history, and the latest user request.
        For continuous sessions, passes only the latest request to maintain fast interactive turns.
        """
        if not messages:
            return "Hello"

        last_content = messages[-1].content

        # If continuing an ongoing conversation session, send only the latest message turn
        # Freebuff CLI retains previous context in its native runtime
        if not is_new_session:
            return last_content

        if len(messages) == 1 and messages[0].role == "user":
            return messages[0].content

        parts = []
        system_prompts = [m.content for m in messages if m.role == "system"]
        if system_prompts:
            parts.append("[System Instructions]\n" + "\n".join(system_prompts))

        history = []
        for m in messages[:-1]:
            if m.role == "user":
                history.append(f"User: {m.content}")
            elif m.role == "assistant":
                history.append(f"Assistant: {m.content}")

        if history:
            parts.append("[Conversation History]\n" + "\n".join(history))

        parts.append(f"[Current Request]\n{last_content}")
        return "\n\n".join(parts)

    async def execute_chat(
        self,
        messages: List[ChatMessage],
        model: Optional[str] = None,
        workspace: Optional[str] = None,
        session_id: Optional[str] = None,
        reset_context: Optional[bool] = None,
        on_chunk: Optional[Callable[[str, bool], None]] = None
    ) -> Dict[str, Any]:
        """
        Executes a chat completion turn across the worker pool, optionally routing to a specific model
        and dynamically binding to the requested project workspace.
        Supports stateful session continuity when session_id matches ongoing conversation.
        """
        if self.worker is None:
            await self.initialize()

        if workspace:
            await self.worker.ensure_workspace(workspace)

        if model:
            resolved = catalog_service.resolve_model(model)
            if resolved and resolved.get("key"):
                await self.worker.ensure_model(resolved["key"])

        # Determine session continuity
        if reset_context is not None:
            should_reset = reset_context
        elif session_id and self.active_session_id == session_id:
            should_reset = False
        else:
            should_reset = True

        if session_id:
            self.active_session_id = session_id
        elif should_reset:
            self.active_session_id = None

        prompt = self.format_messages(messages, is_new_session=should_reset)
        return await self.worker.execute_turn(prompt, reset_context=should_reset, on_chunk=on_chunk)

# Global singleton worker pool
global_pool = WorkerPool()
