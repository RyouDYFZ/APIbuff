"""
Multi-Account Worker Pool & Load Balancing Engine for Freebuff API Gateway.
Supports Round-Robin, Least-Busy, and Quota-Aware load balancing strategies across
multiple isolated Freebuff worker processes, each with dedicated VPN/proxy outbounds.
Features transparent failover on 429 rate limits, network faults, or auth errors.
"""

import time
import asyncio
import logging
from typing import List, Optional, Dict, Any, Callable, Tuple
from .worker import FreebuffWorker
from .models import ChatMessage
from .catalog import catalog_service
from .workspace import sanitize_host_paths
from .accounts import global_account_manager, AccountState
from .proxy_manager import global_proxy_manager
from .exceptions import (
    FreebuffException,
    FreebuffRateLimitError,
    FreebuffAuthError,
    FreebuffServerError,
    FreebuffTimeoutError
)

logger = logging.getLogger("freebuff.pool")

class MultiAccountWorkerPool:
    def __init__(self):
        self.workers: Dict[str, FreebuffWorker] = {}
        self._lock = asyncio.Lock()
        self._rr_index: int = 0
        self.active_session_account: Dict[str, str] = {}  # session_id -> account_id

    @property
    def worker(self) -> Optional[FreebuffWorker]:
        """Backward-compatibility property for routes that expect a single primary worker."""
        if self.workers:
            for w in self.workers.values():
                if w.is_running:
                    return w
            return list(self.workers.values())[0]
        return None

    @property
    def active_session_id(self) -> Optional[str]:
        """Backward-compatibility property reporting active conversation session ID."""
        if self.active_session_account:
            return list(self.active_session_account.keys())[-1]
        return None

    async def initialize(self):
        """Initializes the multi-account worker pool and dedicated proxy tunnels."""
        async with self._lock:
            accounts = global_account_manager.load_accounts()
            for acc in accounts:
                if acc.config.enabled and acc.config.id not in self.workers:
                    worker = FreebuffWorker(
                        account_id=acc.config.id,
                        token=acc.config.token,
                        proxy_url=acc.assigned_proxy,
                        home_dir=acc.home_dir
                    )
                    self.workers[acc.config.id] = worker
                    try:
                        await worker.start()
                        acc.status = "idle"
                    except Exception as e:
                        logger.error(f"Failed to start Freebuff worker for account '{acc.config.id}': {e}")
                        acc.status = "unhealthy"

    async def shutdown(self):
        """Shuts down all workers and proxy core cleanly."""
        async with self._lock:
            for acc_id, worker in list(self.workers.items()):
                try:
                    await worker.stop()
                except Exception:
                    pass
            self.workers.clear()
            global_proxy_manager.stop_singbox()

    def format_messages(self, messages: List[ChatMessage], is_new_session: bool = True) -> str:
        """
        Formats OpenAI chat completion messages array into a prompt suitable for Freebuff.
        Handles system instructions, multi-turn history, and the latest user request.
        For continuous sessions, passes only the latest request to maintain fast interactive turns.
        """
        if not messages:
            return "Hello"

        last_content = messages[-1].content
        if not is_new_session:
            return sanitize_host_paths(last_content)

        if len(messages) == 1 and messages[0].role == "user":
            return sanitize_host_paths(messages[0].content)

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
        return sanitize_host_paths("\n\n".join(parts))

    def select_worker(
        self,
        strategy: Optional[str] = None,
        session_id: Optional[str] = None,
        exclude_accounts: Optional[set] = None
    ) -> Tuple[AccountState, FreebuffWorker]:
        """
        Selects the best worker according to the configured load balancing strategy:
        - least_busy: lowest active in-flight turns
        - round_robin: sequential distribution
        - quota_aware: highest remaining Freebucks balance
        Maintains session stickiness if session_id matches ongoing conversation.
        """
        strat = strategy or global_account_manager.load_balance_strategy
        exclude = exclude_accounts or set()

        # Check session affinity first
        if session_id and session_id in self.active_session_account:
            bound_acc_id = self.active_session_account[session_id]
            if bound_acc_id not in exclude:
                acc_state = global_account_manager.get_account(bound_acc_id)
                worker = self.workers.get(bound_acc_id)
                if acc_state and worker and worker.is_running and not acc_state.is_cooling and acc_state.status != "auth_error":
                    return acc_state, worker

        # Filter candidates
        candidates = []
        for acc in global_account_manager.get_healthy_accounts():
            if acc.config.id in exclude:
                continue
            worker = self.workers.get(acc.config.id)
            if worker and worker.is_running:
                candidates.append((acc, worker))

        if not candidates:
            # Check if all enabled accounts are cooling
            cooling_accounts = [
                acc for acc in global_account_manager.accounts.values()
                if acc.config.enabled and acc.is_cooling
            ]
            if cooling_accounts:
                min_wait = min(acc.cooldown_until - time.time() for acc in cooling_accounts)
                raise FreebuffRateLimitError(
                    f"All Freebuff accounts are currently cooling down due to rate limits (429). Retry in {max(1, int(min_wait))}s."
                )

            # Check if all enabled accounts failed authentication
            auth_error_accounts = [
                acc for acc in global_account_manager.accounts.values()
                if acc.config.enabled and acc.status == "auth_error"
            ]
            if auth_error_accounts:
                raise FreebuffAuthError("All Freebuff accounts failed authentication: tokens are invalid or expired. Run ./login.sh to authenticate.")

            raise FreebuffServerError("No available or healthy Freebuff workers found in pool.")

        # Apply load balancing strategy
        if strat == "least_busy":
            # Sort by active_turns ascending, then weight descending, then last_used ascending
            candidates.sort(key=lambda item: (item[0].active_turns, -item[0].config.weight, item[0].last_used))
            return candidates[0]

        elif strat == "quota_aware":
            # Sort by balance descending (treat None as 0), then active_turns
            candidates.sort(key=lambda item: (-(item[0].balance or 0.0), item[0].active_turns))
            return candidates[0]

        else:  # round_robin default
            selected = candidates[self._rr_index % len(candidates)]
            self._rr_index += 1
            return selected

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
        Executes a chat completion turn across the multi-account worker pool.
        Includes automatic model switching, workspace binding, and transparent failover on 429/errors.
        """
        if not self.workers:
            await self.initialize()

        # Determine session continuity
        should_reset = reset_context if reset_context is not None else (not session_id)

        failed_accounts = set()
        max_attempts = min(3, max(1, len(self.workers)))
        last_exception = None

        prompt = self.format_messages(messages, is_new_session=should_reset)

        for attempt in range(max_attempts):
            try:
                acc_state, worker = self.select_worker(session_id=session_id, exclude_accounts=failed_accounts)
            except Exception as e:
                if last_exception:
                    raise last_exception
                raise

            if workspace:
                await worker.ensure_workspace(workspace)

            if model:
                resolved = catalog_service.resolve_model(model)
                if resolved and resolved.get("key"):
                    await worker.ensure_model(resolved["key"])

            acc_state.active_turns += 1
            acc_state.total_requests += 1
            acc_state.status = "busy"
            acc_state.last_used = time.time()

            try:
                result = await worker.execute_turn(prompt, reset_context=should_reset, on_chunk=on_chunk)
                acc_state.status = "idle"
                if session_id:
                    self.active_session_account[session_id] = acc_state.config.id
                return result

            except FreebuffRateLimitError as e:
                logger.warning(
                    f"Account '{acc_state.config.id}' encountered 429 RateLimit. "
                    f"Marking cooldown 60s and triggering transparent failover (attempt {attempt + 1}/{max_attempts})..."
                )
                acc_state.mark_cooldown(60.0, str(e))
                acc_state.failed_requests += 1
                failed_accounts.add(acc_state.config.id)
                last_exception = e
                if session_id and session_id in self.active_session_account:
                    del self.active_session_account[session_id]

            except (FreebuffAuthError, FreebuffException) as e:
                logger.warning(
                    f"Account '{acc_state.config.id}' encountered error: {e}. "
                    f"Triggering transparent failover (attempt {attempt + 1}/{max_attempts})..."
                )
                acc_state.status = "auth_error" if isinstance(e, FreebuffAuthError) else "error"
                acc_state.failed_requests += 1
                acc_state.last_error = str(e)
                failed_accounts.add(acc_state.config.id)
                last_exception = e
                if session_id and session_id in self.active_session_account:
                    del self.active_session_account[session_id]

            finally:
                acc_state.active_turns = max(0, acc_state.active_turns - 1)
                if acc_state.active_turns == 0 and not acc_state.is_cooling and acc_state.status == "busy":
                    acc_state.status = "idle"

        if last_exception:
            raise last_exception
        raise FreebuffServerError("Chat execution failed across all available account workers.")

# Global singleton worker pool
global_pool = MultiAccountWorkerPool()
