"""Offline pool concurrency and isolation tests. No CLI or upstream is contacted."""

import asyncio
from pathlib import Path
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.accounts import AccountConfig, AccountState
from app.exceptions import FreebuffException, FreebuffRateLimitError, FreebuffTimeoutError
from app.models import ChatMessage
from app.pool import MultiAccountWorkerPool


class FakeWorker:
    def __init__(self, account_id="one", workdir="", **kwargs):
        self.account_id = account_id
        self.workdir = workdir
        self.is_running = True
        self.model = None
        self.context = []
        self.events = []
        self.turns = []
        self.prepare_started = asyncio.Event()
        self.turn_started = asyncio.Event()
        self.stop_started = asyncio.Event()
        self.prepare_release = None
        self.turn_release = None
        self.stop_release = None
        self.error = None
        self.partial = None

    async def start(self):
        self.events.append("start")
        self.is_running = True

    async def stop(self):
        self.events.append("stop")
        self.stop_started.set()
        if self.stop_release:
            await self.stop_release.wait()
        self.is_running = False
        self.context.clear()

    async def ensure_workspace(self, target):
        self.events.append(("workspace", target))
        self.prepare_started.set()
        if self.prepare_release:
            await self.prepare_release.wait()
        self.workdir = target

    async def ensure_model(self, key):
        self.events.append(("model", key))
        self.model = key

    async def execute_turn(self, prompt, reset_context=True, on_chunk=None):
        self.events.append("execute")
        if reset_context:
            self.context.clear()
        self.context.append(prompt)
        self.turns.append({
            "prompt": prompt, "reset": reset_context, "workspace": self.workdir,
            "model": self.model, "context": list(self.context),
        })
        self.turn_started.set()
        if self.turn_release:
            await self.turn_release.wait()
        if self.partial and on_chunk:
            on_chunk(self.partial, False)
        if self.error:
            raise self.error
        return {"content": "\n".join(self.context)}


class PoolSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.manager = MagicMock()
        self.manager.accounts = {}
        self.manager.load_balance_strategy = "least_busy"
        # Deliberately broad results verify the pool's own health checks too.
        self.manager.get_healthy_accounts.side_effect = lambda: list(self.manager.accounts.values())
        self.manager.get_account.side_effect = lambda key: self.manager.accounts.get(key)
        self.patches = [
            patch("app.pool.global_account_manager", self.manager),
            patch("app.pool.WORKER_WORKDIR", self.directory.name),
            patch("app.pool.is_safe_isolated_path", return_value=True),
            patch("app.pool.global_proxy_manager.stop_singbox"),
            patch("app.pool.catalog_service.resolve_model", side_effect=lambda model: {"key": model}),
        ]
        self.mocks = [p.start() for p in self.patches]
        for p in self.patches:
            self.addCleanup(p.stop)
        self.pool = MultiAccountWorkerPool()

    def add_account(self, account_id="one", status="idle"):
        account = AccountState(AccountConfig(id=account_id, name=account_id, token="offline-test-token"))
        account.status = status
        worker = FakeWorker(account_id, str(Path(self.directory.name) / account_id))
        self.manager.accounts[account_id] = account
        self.pool.workers[account_id] = worker
        return account, worker

    @staticmethod
    def messages(text="hello"):
        return [ChatMessage(role="user", content=text)]

    async def wait_until(self, predicate):
        async def wait():
            while not predicate():
                await asyncio.sleep(0.001)
        await asyncio.wait_for(wait(), 1)

    async def test_first_session_and_reset_false_still_replay_full_history(self):
        _, worker = self.add_account()
        history = [
            ChatMessage(role="system", content="system instruction"),
            ChatMessage(role="user", content="previous question"),
            ChatMessage(role="assistant", content="previous answer"),
            ChatMessage(role="user", content="current question"),
        ]
        await self.pool.execute_chat(history, session_id="first-seen", reset_context=False)
        turn = worker.turns[0]
        self.assertTrue(turn["reset"])
        for message in history:
            self.assertIn(message.content, turn["prompt"])
        self.assertEqual(self.pool.active_session_account, {"first-seen": "one"})

    async def test_long_full_history_reaches_worker_with_every_role_and_no_truncation(self):
        account, worker = self.add_account()
        for size in (32 * 1024, 128 * 1024):
            with self.subTest(size=size):
                system = "系统说明：" + "规" * (size // 4)
                developer = "Preserve all conversation details."
                previous_user = "previous question: " + "问" * (size // 4)
                previous_answer = "previous answer: " + "答" * (size // 4)
                current = "current question: " + "😀" * (size // 4) + " END"
                history = [
                    ChatMessage(role="system", content=system),
                    ChatMessage(role="developer", content=developer),
                    ChatMessage(role="user", content=previous_user),
                    ChatMessage(role="assistant", content=previous_answer),
                    ChatMessage(role="user", content=current),
                ]
                expected = (
                    f"[System Instructions]\n{system}\n{developer}\n\n"
                    f"[Conversation History]\nUser: {previous_user}\nAssistant: {previous_answer}\n\n"
                    f"[Current Request]\n{current}"
                )
                result = await self.pool.execute_chat(history, session_id="long-history", reset_context=False)
                turn = worker.turns[-1]
                self.assertTrue(turn["reset"])
                self.assertEqual(turn["prompt"], expected)
                self.assertEqual(turn["context"], [expected])
                self.assertEqual(result["content"], expected)
        self.assertEqual(account.status, "idle")
        self.assertEqual(account.active_turns, 0)
        self.assertEqual(account.failed_requests, 0)
        self.assertEqual(account.total_requests, 2)
        self.assertFalse(self.pool._leased_accounts)

    async def test_long_single_prompt_initializes_empty_pool_and_reaches_worker_intact(self):
        prompt = "😀" * (128 * 1024) + " END"
        created = {}

        async def initialize_pool():
            created["account"], created["worker"] = self.add_account()
            self.pool._initialized = True

        with patch.object(self.pool, "initialize", new_callable=AsyncMock, side_effect=initialize_pool) as initialize:
            result = await self.pool.execute_chat(self.messages(prompt), reset_context=False)
        initialize.assert_awaited_once()
        self.assertEqual(created["worker"].turns[0]["prompt"], prompt)
        self.assertTrue(created["worker"].turns[0]["reset"])
        self.assertEqual(result["content"], prompt)
        self.assertEqual(created["account"].failed_requests, 0)
        self.assertFalse(self.pool._leased_accounts)

    async def test_a_b_a_and_sessionless_turns_never_inherit_other_context(self):
        _, worker = self.add_account()
        await self.pool.execute_chat(self.messages("A private fact"), session_id="A")
        await self.pool.execute_chat(self.messages("B private fact"), session_id="B")
        await self.pool.execute_chat([
            ChatMessage(role="user", content="A private fact"),
            ChatMessage(role="assistant", content="A prior answer"),
            ChatMessage(role="user", content="A continuation"),
        ], session_id="A", reset_context=False)
        await self.pool.execute_chat(self.messages("anonymous"), reset_context=False)
        self.assertNotIn("B private fact", worker.turns[2]["context"][0])
        self.assertIn("A prior answer", worker.turns[2]["context"][0])
        self.assertEqual(worker.turns[3]["context"], ["anonymous"])
        self.assertTrue(all(turn["reset"] for turn in worker.turns))
        self.assertIsNone(self.pool.active_session_id)

    async def test_lease_covers_workspace_model_preparation_and_entire_turn(self):
        acc, worker = self.add_account()
        worker.prepare_release = asyncio.Event()
        first_dir = Path(self.directory.name) / "workspace-a"
        second_dir = Path(self.directory.name) / "workspace-b"
        first_dir.mkdir()
        second_dir.mkdir()
        first = asyncio.create_task(self.pool.execute_chat(self.messages("first"), workspace=str(first_dir), model="model-a"))
        await worker.prepare_started.wait()
        second = asyncio.create_task(self.pool.execute_chat(self.messages("second"), workspace=str(second_dir), model="model-b"))
        await asyncio.sleep(0.01)
        self.assertEqual(acc.active_turns, 1)
        self.assertEqual(worker.events, [("workspace", str(first_dir.resolve()))])
        worker.prepare_release.set()
        await asyncio.gather(first, second)
        self.assertEqual([(t["prompt"], t["workspace"], t["model"]) for t in worker.turns], [
            ("first", str(first_dir.resolve()), "model-a"),
            ("second", str(second_dir.resolve()), "model-b"),
        ])
        self.assertEqual(acc.active_turns, 0)

    async def test_cancellation_during_preparation_stops_before_releasing_lease(self):
        account, worker = self.add_account()
        worker.prepare_release = asyncio.Event()
        worker.stop_release = asyncio.Event()
        request = asyncio.create_task(self.pool.execute_chat(self.messages(), model="requested"))
        await worker.prepare_started.wait()
        request.cancel()
        await worker.stop_started.wait()
        self.assertEqual(account.active_turns, 1)
        self.assertFalse(worker.turns)
        self.assertNotIn(("model", "requested"), worker.events)
        worker.stop_release.set()
        with self.assertRaises(asyncio.CancelledError):
            await request
        self.assertFalse(worker.is_running)
        self.assertFalse(self.pool._leased_accounts)

    async def test_request_workspace_never_becomes_another_requests_default(self):
        _, worker = self.add_account()
        explicit = Path(self.directory.name) / "request-only"
        explicit.mkdir()
        await self.pool.execute_chat(self.messages("first"), workspace=str(explicit))
        await self.pool.execute_chat(self.messages("second"))
        self.assertEqual(worker.turns[0]["workspace"], str(explicit.resolve()))
        self.assertEqual(worker.turns[1]["workspace"], str((Path(self.directory.name) / "one").resolve()))

    async def test_distinct_accounts_can_execute_concurrently(self):
        a, first_worker = self.add_account("one")
        b, second_worker = self.add_account("two")
        release = asyncio.Event()
        first_worker.turn_release = second_worker.turn_release = release
        first = asyncio.create_task(self.pool.execute_chat(self.messages("first")))
        await first_worker.turn_started.wait()
        second = asyncio.create_task(self.pool.execute_chat(self.messages("second")))
        await second_worker.turn_started.wait()
        self.assertEqual((a.active_turns, b.active_turns), (1, 1))
        release.set()
        await asyncio.gather(first, second)

    async def test_cancelled_waiter_never_stops_the_active_worker(self):
        account, worker = self.add_account()
        worker.turn_release = asyncio.Event()
        first = asyncio.create_task(self.pool.execute_chat(self.messages("active")))
        await worker.turn_started.wait()
        queued = asyncio.create_task(self.pool.execute_chat(self.messages("queued")))
        await asyncio.sleep(0.01)
        queued.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await queued
        self.assertNotIn("stop", worker.events)
        self.assertEqual(account.active_turns, 1)
        worker.turn_release.set()
        self.assertEqual((await first)["content"], "active")

    async def test_cancelled_owner_keeps_lease_until_stop_finishes(self):
        account, worker = self.add_account()
        worker.turn_release = asyncio.Event()
        worker.stop_release = asyncio.Event()
        active = asyncio.create_task(self.pool.execute_chat(self.messages(), session_id="A"))
        await worker.turn_started.wait()
        active.cancel()
        await worker.stop_started.wait()
        active.cancel()  # Repeated cancellation must not interrupt process cleanup.
        await asyncio.sleep(0.01)
        self.assertFalse(active.done())
        self.assertEqual(account.active_turns, 1)
        self.assertEqual(self.pool._leased_accounts, {"one"})
        worker.stop_release.set()
        with self.assertRaises(asyncio.CancelledError):
            await active
        self.assertFalse(worker.is_running)
        self.assertEqual(account.status, "error")
        self.assertEqual(account.active_turns, 0)
        self.assertFalse(self.pool._leased_accounts)
        self.assertNotIn("A", self.pool.active_session_account)

    async def test_queue_deadline_returns_typed_429_without_account_penalty(self):
        account, worker = self.add_account()
        worker.turn_release = asyncio.Event()
        active = asyncio.create_task(self.pool.execute_chat(self.messages("active")))
        await worker.turn_started.wait()
        with patch.dict(os.environ, {"FREEBUFF_QUEUE_TIMEOUT": "0.025"}):
            with self.assertRaises(FreebuffRateLimitError) as raised:
                await self.pool.execute_chat(self.messages("queued"))
        self.assertEqual(raised.exception.status_code, 429)
        self.assertEqual(raised.exception.error_code, "queue_timeout")
        self.assertEqual(account.total_requests, 1)
        self.assertEqual(account.failed_requests, 0)
        self.assertNotIn("stop", worker.events)
        worker.turn_release.set()
        await active

    async def test_unknown_model_is_rejected_without_leasing_or_reconfiguring_worker(self):
        account, worker = self.add_account()
        worker.model = "previous-model"
        self.mocks[4].side_effect = None
        self.mocks[4].return_value = None
        with self.assertRaises(FreebuffException) as raised:
            await self.pool.execute_chat(self.messages(), model="unknown-model")
        self.assertEqual(raised.exception.status_code, 404)
        self.assertEqual(raised.exception.error_code, "model_not_found")
        self.assertEqual(worker.model, "previous-model")
        self.assertFalse(worker.events)
        self.assertEqual(account.status, "idle")
        self.assertEqual((account.total_requests, account.failed_requests), (0, 0))
        self.assertFalse(self.pool._leased_accounts)

    async def test_turn_deadline_includes_workspace_preparation_and_waits_for_cleanup(self):
        account, worker = self.add_account()
        worker.prepare_release = asyncio.Event()
        worker.stop_release = asyncio.Event()
        with patch("app.pool.TURN_TIMEOUT_SECONDS", 0.025):
            active = asyncio.create_task(self.pool.execute_chat(self.messages()))
            await asyncio.wait_for(worker.prepare_started.wait(), 1)
            await asyncio.wait_for(worker.stop_started.wait(), 1)
            self.assertFalse(active.done())
            self.assertEqual(self.pool._leased_accounts, {"one"})
            self.assertEqual(account.active_turns, 1)
            self.assertNotIn("execute", worker.events)
            worker.stop_release.set()
            with self.assertRaises(FreebuffTimeoutError):
                await active
        self.assertFalse(worker.is_running)
        self.assertEqual(account.status, "error")
        self.assertEqual((account.active_turns, account.failed_requests), (0, 1))
        self.assertFalse(self.pool._leased_accounts)

    async def test_preparation_and_generation_share_one_turn_budget(self):
        account, worker = self.add_account()
        worker.turn_release = asyncio.Event()
        original_prepare = worker.ensure_workspace

        async def slow_prepare(target):
            await original_prepare(target)
            await asyncio.sleep(0.04)

        async def complete_generation():
            await worker.turn_started.wait()
            await asyncio.sleep(0.06)
            worker.turn_release.set()

        worker.ensure_workspace = slow_prepare
        completion = asyncio.create_task(complete_generation())
        try:
            with patch("app.pool.TURN_TIMEOUT_SECONDS", 0.08):
                with self.assertRaises(FreebuffTimeoutError):
                    await self.pool.execute_chat(self.messages())
        finally:
            completion.cancel()
            await asyncio.gather(completion, return_exceptions=True)
        self.assertTrue(worker.turn_started.is_set())
        self.assertFalse(worker.is_running)
        self.assertEqual(account.active_turns, 0)

    async def test_refusals_and_server_failures_never_retry_another_account(self):
        for status, expected in ((401, "auth_error"), (402, "payment_error"), (403, "permission_error"), (429, "rate_limited"), (502, "error")):
            with self.subTest(status=status):
                self.pool = MultiAccountWorkerPool()
                self.manager.accounts.clear()
                account, worker = self.add_account("one")
                other, unused = self.add_account("two")
                error = FreebuffException("upstream rejection", status_code=status)
                worker.error = error
                worker.partial = "already emitted"
                chunks = []
                self.pool.active_session_account["session"] = "one"
                self.pool._worker_session_owner["one"] = "session"
                with self.assertRaises(FreebuffException) as raised:
                    await self.pool.execute_chat(self.messages(), session_id="session", on_chunk=lambda text, reasoning: chunks.append(text))
                self.assertIs(raised.exception, error)
                self.assertEqual(chunks, ["already emitted"])
                self.assertEqual(account.status, expected)
                self.assertEqual(account.failed_requests, 1)
                self.assertEqual(other.total_requests, 0)
                self.assertFalse(unused.events)
                self.assertFalse(worker.is_running)
                self.assertNotIn("session", self.pool.active_session_account)
                self.assertNotIn("one", self.pool._worker_session_owner)

    async def test_429_uses_only_explicit_retry_after_and_new_request_may_recover(self):
        account, worker = self.add_account()
        error = FreebuffRateLimitError("retry later")
        error.retry_after = 7.5
        worker.error = error
        with self.assertRaises(FreebuffRateLimitError):
            await self.pool.execute_chat(self.messages())
        self.assertEqual(account.status, "cooling")
        self.assertGreater(account.cooldown_until - time.time(), 7)
        self.assertLessEqual(account.cooldown_until - time.time(), 7.5)
        with self.assertRaises(FreebuffRateLimitError):
            self.pool.select_worker()
        account.cooldown_until = time.time() - 1
        worker.error = None
        await self.pool.execute_chat(self.messages("explicit later request"))
        self.assertIn("start", worker.events)
        self.assertEqual(len(worker.turns), 2)
        self.assertEqual(worker.turns[-1]["context"], ["explicit later request"])

    async def test_429_without_retry_after_is_excluded_until_reload(self):
        account, worker = self.add_account()
        worker.error = FreebuffRateLimitError("limit")
        with self.assertRaises(FreebuffRateLimitError):
            await self.pool.execute_chat(self.messages())
        self.assertEqual(account.cooldown_until, 0)
        self.assertEqual(account.status, "rate_limited")
        with self.assertRaises(FreebuffRateLimitError):
            await self.pool.execute_chat(self.messages("another request"))
        self.assertEqual(len(worker.turns), 1)

    async def test_unhealthy_account_affinity_never_bypasses_health_checks(self):
        account, _ = self.add_account("one")
        _, healthy = self.add_account("two")
        self.pool.active_session_account["bound"] = "one"
        for status in ("auth_error", "permission_error", "payment_error", "error", "unhealthy", "disabled", "rate_limited"):
            with self.subTest(status=status):
                account.status = status
                self.assertIs(self.pool.select_worker(session_id="bound")[1], healthy)
        account.status = "idle"
        account.config.enabled = False
        self.assertIs(self.pool.select_worker(session_id="bound")[1], healthy)

    async def test_reload_waits_for_turn_before_stopping_loading_or_switching_proxy(self):
        old_account, old_worker = self.add_account()
        old_worker.turn_release = asyncio.Event()
        active = asyncio.create_task(self.pool.execute_chat(self.messages("old turn")))
        await old_worker.turn_started.wait()
        new_account = AccountState(AccountConfig(id="one", name="replacement", token="new-test-token"))
        def load():
            self.assertFalse(old_worker.is_running)
            self.assertEqual(old_account.active_turns, 0)
            self.manager.accounts = {"one": new_account}
            return [new_account]
        self.manager.load_accounts.side_effect = load
        with patch("app.pool.FreebuffWorker", FakeWorker):
            reload_task = asyncio.create_task(self.pool.reload())
            await self.wait_until(lambda: self.pool._maintaining)
            queued = asyncio.create_task(self.pool.execute_chat(self.messages("replacement turn")))
            await asyncio.sleep(0.01)
            self.manager.load_accounts.assert_not_called()
            self.mocks[3].assert_not_called()
            self.assertNotIn("stop", old_worker.events)
            old_worker.turn_release.set()
            await active
            await reload_task
            await queued
        new_worker = self.pool.workers["one"]
        self.assertIsNot(new_worker, old_worker)
        self.assertEqual(len(old_worker.turns), 1)
        self.assertEqual(new_worker.turns[0]["prompt"], "replacement turn")

    async def test_maintenance_timeout_does_not_interrupt_the_active_turn(self):
        account, worker = self.add_account()
        worker.turn_release = asyncio.Event()
        active = asyncio.create_task(self.pool.execute_chat(self.messages()))
        await worker.turn_started.wait()
        with patch("app.pool.TURN_TIMEOUT_SECONDS", -9.975):
            with self.assertRaises(FreebuffException) as raised:
                await self.pool.reload()
        self.assertEqual(raised.exception.error_code, "maintenance_timeout")
        self.assertEqual(raised.exception.status_code, 504)
        self.manager.load_accounts.assert_not_called()
        self.assertNotIn("stop", worker.events)
        self.assertFalse(self.pool._maintaining)
        self.assertEqual(account.active_turns, 1)
        worker.turn_release.set()
        await active

    async def test_shutdown_refuses_new_leases_then_waits_for_active_turn(self):
        _, worker = self.add_account()
        worker.turn_release = asyncio.Event()
        active = asyncio.create_task(self.pool.execute_chat(self.messages()))
        await worker.turn_started.wait()
        shutdown = asyncio.create_task(self.pool.shutdown())
        await self.wait_until(lambda: self.pool._closing)
        with self.assertRaises(FreebuffException) as raised:
            await self.pool.execute_chat(self.messages("refused"))
        self.assertEqual(raised.exception.status_code, 503)
        self.assertNotIn("stop", worker.events)
        worker.turn_release.set()
        await active
        await shutdown
        self.assertFalse(self.pool.workers)
        self.assertFalse(worker.is_running)

    async def test_workspace_maintenance_waits_and_updates_explicit_default(self):
        _, worker = self.add_account()
        target = Path(self.directory.name) / "explicit"
        target.mkdir()
        worker.turn_release = asyncio.Event()
        active = asyncio.create_task(self.pool.execute_chat(self.messages("old"), session_id="old"))
        await worker.turn_started.wait()
        switch = asyncio.create_task(self.pool.set_workspace(str(target)))
        await self.wait_until(lambda: self.pool._maintaining)
        self.assertNotEqual(worker.workdir, str(target.resolve()))
        worker.turn_release.set()
        await active
        self.assertEqual(await switch, str(target.resolve()))
        self.assertIsNone(self.pool.active_session_id)
        await self.pool.execute_chat(self.messages("new"))
        self.assertEqual(worker.turns[-1]["workspace"], str(target.resolve()))

    async def test_defaults_are_per_account_and_block_shared_symlinks(self):
        self.add_account("one")
        self.add_account("two")
        await self.pool.execute_chat(self.messages("one"))
        await self.pool.execute_chat(self.messages("two"))
        first, second = self.pool.workers.values()
        self.assertEqual(first.workdir, str((Path(self.directory.name) / "one").resolve()))
        self.assertEqual(second.workdir, str((Path(self.directory.name) / "two").resolve()))
        (Path(self.directory.name) / "alias").symlink_to(first.workdir)
        with self.assertRaises(ValueError):
            self.pool._default_workspace("alias")

    async def test_initialization_load_and_proxy_stop_run_off_event_loop(self):
        main_thread = threading.get_ident()
        observed = []
        account = AccountState(AccountConfig(id="one", name="one", token="offline-token"))
        def load():
            observed.append(threading.get_ident())
            self.manager.accounts = {"one": account}
            return [account]
        self.manager.load_accounts.side_effect = load
        self.mocks[3].side_effect = lambda: observed.append(threading.get_ident())
        with patch("app.pool.FreebuffWorker", FakeWorker):
            await self.pool.initialize()
        self.assertEqual(self.pool.worker.workdir, str((Path(self.directory.name) / "one").resolve()))
        await self.pool.shutdown()
        self.assertEqual(len(observed), 2)
        self.assertTrue(all(thread != main_thread for thread in observed))

    async def test_cancelled_reload_keeps_gate_until_background_load_finishes(self):
        entered, release = threading.Event(), threading.Event()
        def load():
            entered.set()
            release.wait(2)
            return []
        self.manager.load_accounts.side_effect = load
        reload_task = asyncio.create_task(self.pool.reload())
        try:
            await self.wait_until(entered.is_set)
            reload_task.cancel()
            await asyncio.sleep(0.01)
            self.assertTrue(self.pool._maintaining)
            self.assertFalse(reload_task.done())
        finally:
            release.set()
        with self.assertRaises(asyncio.CancelledError):
            await reload_task
        self.assertFalse(self.pool._maintaining)


if __name__ == "__main__":
    unittest.main()
