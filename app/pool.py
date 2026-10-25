"""Exclusive worker leases, isolated chat turns, and coordinated pool maintenance."""

import asyncio
from contextlib import asynccontextmanager
import logging
import math
import os
import random
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from .accounts import AccountState, global_account_manager
from .catalog import catalog_service
from .config import SESSION_BOOT_TIMEOUT_SECONDS, TURN_TIMEOUT_SECONDS, WORKER_WORKDIR
from .exceptions import (
    FreebuffAuthError,
    FreebuffException,
    FreebuffPaymentError,
    FreebuffPermissionError,
    FreebuffRateLimitError,
    FreebuffServerError,
    FreebuffTimeoutError,
)
from .models import ChatMessage
from .proxy_manager import global_proxy_manager
from .worker import FreebuffWorker, validate_turn_prompt
from .workspace import is_safe_isolated_path, sanitize_host_paths

logger = logging.getLogger("freebuff.pool")

# Upstream answers that say nothing about account health (client disconnects,
# timeouts, 5xx, request errors): cool off briefly and let the worker restart
# on its next lease. Sticky account verdicts remain 401/402/403; anything else
# used to require a manual reload, so one dropped connection could take a
# single-account pool down entirely.
TRANSIENT_COOLDOWN_SECONDS = 30.0
TERMINAL_ACCOUNT_STATUS = {401: "auth_error", 402: "payment_error", 403: "permission_error"}


async def _finish_before_cancel(awaitable):
    """Keep a lease/gate until its cleanup or maintenance mutation really finishes."""
    task = asyncio.ensure_future(awaitable)
    cancelled = None
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError as exc:
            if task.done() and task.cancelled():
                raise
            cancelled = exc
    if cancelled is not None:
        raise cancelled
    return result


class MultiAccountWorkerPool:
    def __init__(self):
        self.workers: Dict[str, FreebuffWorker] = {}
        self._lock = asyncio.Lock()  # Serializes lifecycle/workspace maintenance.
        self._condition = asyncio.Condition()
        self._worker_locks: Dict[str, asyncio.Lock] = {}
        self._leased_accounts = set()
        self._maintaining = False
        self._closing = False
        self._initialized = False
        self._workspace_override: Optional[str] = None
        self._rr_index = 0
        # Session IDs are account affinity only. Every turn replays and resets.
        self.active_session_account: Dict[str, str] = {}
        self._worker_session_owner: Dict[str, str] = {}

    @property
    def worker(self) -> Optional[FreebuffWorker]:
        """Backward-compatible primary worker for monitoring routes."""
        if self.workers:
            return next((w for w in self.workers.values() if w.is_running), next(iter(self.workers.values())))
        return None

    @property
    def active_session_id(self) -> Optional[str]:
        return next(reversed(self._worker_session_owner.values()), None)

    async def _wait_for_idle(self):
        async with self._condition:
            await self._condition.wait_for(lambda: not self._leased_accounts)

    async def _end_maintenance(self):
        async with self._condition:
            self._maintaining = False
            self._condition.notify_all()

    @asynccontextmanager
    async def _maintenance(self, *, closing=False):
        async with self._lock:
            async with self._condition:
                if self._closing and not closing:
                    raise FreebuffServerError("Worker pool is shutting down.", status_code=503)
                self._maintaining = True
                if closing:
                    self._closing = True
                self._condition.notify_all()
            try:
                try:
                    await asyncio.wait_for(self._wait_for_idle(), TURN_TIMEOUT_SECONDS + 10)
                except asyncio.TimeoutError:
                    raise FreebuffException(
                        "Pool maintenance timed out waiting for active turns.",
                        status_code=504,
                        error_type="timeout_error",
                        error_code="maintenance_timeout",
                    ) from None
                yield
            finally:
                await _finish_before_cancel(self._end_maintenance())

    @staticmethod
    def _default_workspace(account_id: str) -> str:
        root = os.path.realpath(os.path.expanduser(WORKER_WORKDIR))
        requested = os.path.join(root, account_id)
        if os.path.islink(requested):
            raise ValueError("Account workspaces must not be symlinks")
        target = os.path.realpath(requested)
        if os.path.commonpath((root, target)) != root or target == root:
            raise ValueError("Account workspace must remain inside the worker workspace root")
        os.makedirs(target, mode=0o700, exist_ok=True)
        if not is_safe_isolated_path(target):
            raise ValueError("Account workspace is not an allowed isolated path")
        return target

    @staticmethod
    def _validated_workspace(target: str) -> str:
        if not target or not target.strip():
            raise FreebuffException("Workspace must be a non-empty path.", status_code=400, error_type="invalid_request_error", error_code="invalid_workspace")
        normalized = os.path.realpath(os.path.expanduser(target))
        if not os.path.isdir(normalized):
            raise FreebuffException(
                "Workspace directory does not exist.", status_code=400,
                error_type="invalid_request_error", error_code="invalid_workspace",
            )
        if not is_safe_isolated_path(normalized):
            raise FreebuffPermissionError("Workspace is outside the allowed isolated roots.", error_code="workspace_not_allowed")
        return normalized

    def _invalidate_owner(self, account_id: str, *, clear_affinity=False):
        self._worker_session_owner.pop(account_id, None)
        if clear_affinity:
            self.active_session_account = {
                session: owner for session, owner in self.active_session_account.items()
                if owner != account_id
            }

    async def _stop_all(self):
        failures = []
        for account_id, worker in self.workers.items():
            self._invalidate_owner(account_id, clear_affinity=True)
            account = global_account_manager.get_account(account_id)
            if account is not None:
                account.status = "error"
            try:
                await worker.stop()
            except Exception as exc:
                failures.append(exc)
                logger.error("Failed to stop worker for account '%s'.", account_id)
        if failures:
            # Do not start replacement processes/proxies while an old one may live.
            raise FreebuffServerError("Could not stop every worker during pool maintenance.") from failures[0]
        self.workers.clear()
        self._worker_locks.clear()
        await asyncio.to_thread(global_proxy_manager.stop_singbox)

    async def _load_workers(self):
        # Loading also seeds credentials, fetches subscriptions and starts proxies.
        accounts = await asyncio.to_thread(global_account_manager.load_accounts)
        for acc in accounts:
            if not self._healthy(acc):
                continue
            worker = None
            try:
                workdir = (
                    await asyncio.to_thread(self._validated_workspace, self._workspace_override)
                    if self._workspace_override else
                    await asyncio.to_thread(self._default_workspace, acc.config.id)
                )
                worker = FreebuffWorker(
                    account_id=acc.config.id, token=acc.config.token,
                    proxy_url=acc.assigned_proxy, home_dir=acc.home_dir, workdir=workdir,
                )
                self.workers[acc.config.id] = worker
                self._worker_locks[acc.config.id] = asyncio.Lock()
                await worker.start()
                acc.status = "idle"
            except Exception:
                logger.error("Failed to start Freebuff worker for account '%s'.", acc.config.id)
                acc.status = "unhealthy"
                acc.last_error = "Worker startup failed; reload after correcting its configuration."
                if worker is not None:
                    await worker.stop()
        self._initialized = True

    async def initialize(self):
        """Load/start the initial pool without changing an already initialized pool."""
        async with self._maintenance():
            if self._initialized or self.workers:
                return
            self._initialized = True
            await _finish_before_cancel(self._load_workers())

    async def reload(self):
        """Drain turns, stop old workers/proxies, then load and start replacements."""
        async def replace():
            # Failed explicit reloads must not cause lazy, repeated config retries.
            self._initialized = True
            await self._stop_all()
            await self._load_workers()
        async with self._maintenance():
            await _finish_before_cancel(replace())

    async def shutdown(self):
        """Refuse new leases before draining and stopping workers and proxy core."""
        async with self._condition:
            self._closing = True
            self._condition.notify_all()
        async with self._maintenance(closing=True):
            await _finish_before_cancel(self._stop_all())

    async def set_workspace(self, target: str) -> str:
        """Set an explicit pool default only while all worker leases are drained."""
        normalized = await asyncio.to_thread(self._validated_workspace, target)
        if not self._initialized and not self.workers:
            await self.initialize()

        async def switch():
            for account_id, worker in self.workers.items():
                self._invalidate_owner(account_id)
                acc = global_account_manager.get_account(account_id)
                if acc is None or not self._healthy(acc):
                    continue
                try:
                    await worker.ensure_workspace(normalized)
                except Exception as exc:
                    self._record_failure(acc, exc)
                    await self._stop_failed_worker(worker)
                    raise
            self._workspace_override = normalized

        async with self._maintenance():
            await _finish_before_cancel(switch())
        return normalized

    def format_messages(self, messages: List[ChatMessage], is_new_session: bool = True) -> str:
        """Format history; execute_chat always requests the complete conversation."""
        if not messages:
            return "Hello"
        last_content = messages[-1].content
        if not is_new_session:
            return sanitize_host_paths(last_content)
        if len(messages) == 1 and messages[0].role == "user":
            return sanitize_host_paths(messages[0].content)
        parts = []
        system_prompts = [m.content for m in messages if m.role in ("system", "developer")]
        if system_prompts:
            parts.append("[System Instructions]\n" + "\n".join(system_prompts))
        history = []
        for message in messages[:-1]:
            if message.role in ("user", "assistant"):
                history.append(f"{message.role.capitalize()}: {message.content}")
        if history:
            parts.append("[Conversation History]\n" + "\n".join(history))
        parts.append(f"[Current Request]\n{last_content}")
        return sanitize_host_paths("\n\n".join(parts))

    @staticmethod
    def _healthy(account: AccountState) -> bool:
        return (
            account.config.enabled and not account.is_cooling
            and account.status in {"idle", "busy", "cooling"}
        )

    def _candidates(self, exclude_accounts=None):
        excluded = exclude_accounts or set()
        return [
            (acc, self.workers[acc.config.id])
            for acc in global_account_manager.get_healthy_accounts()
            if self._healthy(acc) and acc.config.id not in excluded and acc.config.id in self.workers
        ]

    def select_worker(
        self, strategy: Optional[str] = None, session_id: Optional[str] = None,
        exclude_accounts: Optional[set] = None,
    ) -> Tuple[AccountState, FreebuffWorker]:
        """Select account affinity or load balance, excluding unhealthy accounts."""
        candidates = self._candidates(exclude_accounts)
        if not candidates:
            accounts = [a for a in global_account_manager.accounts.values() if a.config.enabled]
            cooling = [a for a in accounts if a.is_cooling]
            if cooling:
                retry_after = max(0, min(a.cooldown_until - time.time() for a in cooling))
                raise FreebuffRateLimitError(
                    "All available Freebuff accounts are cooling down.", retry_after=retry_after,
                )
            if any(a.status == "rate_limited" for a in accounts):
                raise FreebuffRateLimitError("Freebuff account rate limit requires an explicit account reload.")
            if any(a.status == "auth_error" for a in accounts):
                raise FreebuffAuthError("No healthy Freebuff workers; authentication must be restored before reload.")
            if any(a.status == "payment_error" for a in accounts):
                raise FreebuffPaymentError("Freebuff account payment error: hours or credits exhausted; explicit account reload or top-up required.")
            if any(a.status == "permission_error" for a in accounts):
                raise FreebuffPermissionError("Freebuff account permission error: proxy node or region blocked; update configuration and reload.")
            raise FreebuffServerError("No available or healthy Freebuff workers found in pool.", status_code=503)

        bound = self.active_session_account.get(session_id) if session_id else None
        if bound:
            for acc, worker in candidates:
                if acc.config.id == bound:
                    return acc, worker
        strat = strategy or global_account_manager.load_balance_strategy
        if strat == "least_busy":
            return min(candidates, key=lambda item: (item[0].active_turns, -item[0].config.weight, item[0].last_used))
        if strat == "quota_aware":
            return min(candidates, key=lambda item: (-(item[0].balance or 0.0), item[0].active_turns))
        selected = candidates[self._rr_index % len(candidates)]
        self._rr_index += 1
        return selected

    async def _acquire_worker(self, session_id):
        try:
            timeout = float(os.getenv("FREEBUFF_QUEUE_TIMEOUT", "15"))
            if not math.isfinite(timeout) or timeout < 0:
                raise ValueError
        except ValueError:
            timeout = 15.0
        deadline = asyncio.get_running_loop().time() + timeout
        async with self._condition:
            while True:
                if self._closing:
                    raise FreebuffServerError("Worker pool is shutting down.", status_code=503)
                if not self._maintaining:
                    if self._candidates(self._leased_accounts):
                        acc, worker = self.select_worker(session_id=session_id, exclude_accounts=self._leased_accounts)
                        lease = self._worker_locks.setdefault(acc.config.id, asyncio.Lock())
                        # Only this gate grants leases, so acquire cannot block here.
                        await lease.acquire()
                        self._leased_accounts.add(acc.config.id)
                        acc.active_turns += 1
                        acc.total_requests += 1
                        acc.status = "busy"
                        acc.last_used = time.time()
                        return acc, worker
                    if not self._candidates():
                        self.select_worker(session_id=session_id)  # Raise the typed unavailable error.
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise FreebuffRateLimitError("Timed out waiting for a Freebuff worker.", error_code="queue_timeout")
                try:
                    await asyncio.wait_for(self._condition.wait(), remaining)
                except asyncio.TimeoutError:
                    raise FreebuffRateLimitError(
                        "Timed out waiting for a Freebuff worker.", error_code="queue_timeout",
                    ) from None

    async def _release_worker(self, acc):
        async with self._condition:
            acc.active_turns = max(0, acc.active_turns - 1)
            if acc.status == "busy":
                acc.status = "idle"
            self._leased_accounts.remove(acc.config.id)
            self._worker_locks[acc.config.id].release()
            self._condition.notify_all()

    def _record_failure(self, acc, error):
        self._invalidate_owner(acc.config.id, clear_affinity=True)
        acc.failed_requests += 1
        acc.last_error = str(error)
        status = getattr(error, "status_code", None)
        if status == 429:
            retry_after = getattr(error, "retry_after", None)
            if isinstance(retry_after, (int, float)) and not isinstance(retry_after, bool) and math.isfinite(retry_after) and retry_after >= 0:
                acc.mark_cooldown(retry_after, str(error))
            else:
                acc.cooldown_until = 0
                acc.status = "rate_limited"
        elif status in TERMINAL_ACCOUNT_STATUS:
            # Sticky account-level verdicts: credentials, balance, or access.
            acc.cooldown_until = 0
            acc.status = TERMINAL_ACCOUNT_STATUS[status]
        else:
            acc.mark_cooldown(TRANSIENT_COOLDOWN_SECONDS, str(error))

    @staticmethod
    async def _stop_failed_worker(worker):
        try:
            await worker.stop()
        except Exception:
            logger.error("Failed to stop a worker after an interrupted turn.")
            raise

    @staticmethod
    def _planned_boots(worker, target: str, model_key) -> int:
        """Cold starts this lease may need: worker boot, workspace or model switch."""
        boots = 0
        if not worker.is_running:
            boots += 1
        worker_workdir = os.path.realpath(os.path.abspath(worker.workdir or ""))
        target_workdir = os.path.realpath(os.path.abspath(target or ""))
        if worker_workdir != target_workdir:
            boots += 1
        current_model = getattr(worker, "current_model_key", None)
        if model_key and (current_model is None or current_model() != model_key):
            boots += 1
        return boots

    @staticmethod
    def _turn_budget(boots: int) -> float:
        """One generation budget plus a boot allowance per planned cold start."""
        if TURN_TIMEOUT_SECONDS <= 1.0 or boots <= 0:
            return TURN_TIMEOUT_SECONDS
        return TURN_TIMEOUT_SECONDS + boots * (SESSION_BOOT_TIMEOUT_SECONDS + 5.0)

    async def execute_chat(
        self, messages: List[ChatMessage], model: Optional[str] = None,
        workspace: Optional[str] = None, session_id: Optional[str] = None,
        reset_context: Optional[bool] = None,
        on_chunk: Optional[Callable[[str, bool], None]] = None,
    ) -> Dict[str, Any]:
        """Run exactly once, with a fresh context and a lease covering all mutations."""
        prompt = validate_turn_prompt(self.format_messages(messages, is_new_session=True))
        normalized = await asyncio.to_thread(self._validated_workspace, workspace) if workspace else None
        model_key = None
        if model:
            # Resolve before leasing: a bad model is a request error, not an
            # account failure, and must never silently retain the previous model.
            resolved = await asyncio.to_thread(catalog_service.resolve_model, model)
            if not resolved or not resolved.get("key"):
                raise FreebuffException(
                    f"Model '{model}' was not found in the catalog.", status_code=404,
                    error_type="invalid_request_error", error_code="model_not_found",
                )
            model_key = resolved["key"]
        if not self._initialized and not self.workers:
            await self.initialize()
        acc, worker = await self._acquire_worker(session_id)
        self._invalidate_owner(acc.config.id)
        target = normalized or self._workspace_override or await asyncio.to_thread(self._default_workspace, acc.config.id)
        budget = self._turn_budget(self._planned_boots(worker, target, model_key))

        async def run_leased_turn():
            if not worker.is_running:
                await worker.start()
            await worker.ensure_workspace(target)
            if model_key:
                await worker.ensure_model(model_key)
            # Client reset_context=False cannot assert exclusive ownership of a CLI.
            return await worker.execute_turn(prompt, reset_context=True, on_chunk=on_chunk)

        try:
            try:
                # One budget covers process startup, workspace/model changes,
                # input and generation. Cleanup may outlive it while the lease
                # stays held; no successor may reuse a half-stopped worker.
                result = await asyncio.wait_for(run_leased_turn(), budget)
            except asyncio.TimeoutError:
                raise FreebuffTimeoutError() from None
            if session_id:
                self.active_session_account[session_id] = acc.config.id
                self._worker_session_owner[acc.config.id] = session_id
            return result
        except BaseException as error:
            self._record_failure(acc, error)
            try:
                await _finish_before_cancel(self._stop_failed_worker(worker))
            except asyncio.CancelledError:
                # Cleanup survived the cancellation and finished; the transient
                # verdict from _record_failure already applies.
                raise
            except Exception:
                # Preserve the upstream error; a worker with uncertain cleanup is
                # never eligible for another lease, even after a timed cooldown.
                acc.status = "error"
            raise
        finally:
            await _finish_before_cancel(self._release_worker(acc))


# Global singleton worker pool
global_pool = MultiAccountWorkerPool()
