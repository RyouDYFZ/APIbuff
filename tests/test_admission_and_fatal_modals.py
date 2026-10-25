"""
Tests for New 1 (Admission Modal Auto-Confirmation) and New 2 (Fatal Error Modals Fast-Fail).
"""

import asyncio
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from app.worker import FreebuffWorker
from app.exceptions import (
    FreebuffAuthError,
    FreebuffPermissionError,
    FreebuffPaymentError,
    FreebuffRateLimitError,
    FreebuffInteractionRequiredError,
)
from app.accounts import AccountConfig, AccountState
from app.pool import MultiAccountWorkerPool


class TestAdmissionAndFatalModals(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.worker = FreebuffWorker(workdir="/tmp/test_admission_workdir")
        self.worker.master_fd = 999  # Mock fd
        self.worker.is_running = True
        self.worker.is_authenticated = True

    async def test_admission_confirm_modal_auto_confirms(self):
        """New 1: Ensure modal 'Enter: confirm and send' triggers auto-confirm with \\r."""
        written = []

        async def mock_write_all(fd, data):
            written.append((fd, data))

        with patch("app.worker.write_all", mock_write_all), \
             patch("app.worker.ADMISSION_AUTO_CONFIRM", True), \
             patch("random.uniform", return_value=0.01):
            
            # Feed admission confirm prompt into raw_buffer
            chunk = b"\x1b[38;5;4mEnter: confirm and send\x1b[0m"
            with patch("os.read", return_value=chunk):
                self.worker._on_pty_readable()

            self.assertTrue(self.worker._admission_scheduled)
            self.assertIsNotNone(self.worker._admission_task)

            # Wait for task to finish
            await self.worker._admission_task

            self.assertFalse(self.worker._admission_scheduled)
            self.assertEqual(written, [(999, b"\r")])

    async def test_admission_retry_modal_auto_retries_bounded(self):
        """New 1: Ensure modal 'Enter: retry' retries up to 2 times, then stops."""
        written = []

        async def mock_write_all(fd, data):
            written.append((fd, data))

        with patch("app.worker.write_all", mock_write_all), \
             patch("app.worker.ADMISSION_AUTO_CONFIRM", True), \
             patch("random.uniform", return_value=0.01):

            # Attempt 1
            with patch("os.read", return_value=b"Enter: retry"):
                self.worker._on_pty_readable()
            self.assertEqual(self.worker._admission_retry_count, 1)
            await self.worker._admission_task

            # Attempt 2
            with patch("os.read", return_value=b"Enter: retry"):
                self.worker._on_pty_readable()
            self.assertEqual(self.worker._admission_retry_count, 2)
            await self.worker._admission_task

            # Attempt 3 (exceeds limit)
            with patch("os.read", return_value=b"Enter: retry"):
                self.worker._on_pty_readable()
            self.assertFalse(self.worker._admission_scheduled)

            self.assertEqual(len(written), 2)

    async def test_admission_confirm_when_disabled_raises_interaction_required(self):
        """New 1: When ADMISSION_AUTO_CONFIRM=False, _check_interaction raises interaction required."""
        self.worker._raw_buffer.extend(b"Enter: confirm and send")
        with patch("app.worker.ADMISSION_AUTO_CONFIRM", False):
            with self.assertRaises(FreebuffInteractionRequiredError) as caught:
                self.worker._check_interaction()
            self.assertIn("admission requires explicit user confirmation", str(caught.exception))

    async def test_fatal_modal_account_suspended_raises_auth_error_immediately(self):
        """New 2: 'This account is suspended' unblocks ready_event and raises FreebuffAuthError."""
        chunk = b"This account is suspended. To appeal, visit https://freebuff.com/account?tab=standing."
        with patch("os.read", return_value=chunk):
            self.worker._on_pty_readable()

        self.assertTrue(self.worker.ready_event.is_set())
        with self.assertRaises(FreebuffAuthError) as caught:
            self.worker._check_interaction()
        self.assertEqual(caught.exception.error_code, "account_suspended")
        self.assertFalse(self.worker.is_authenticated)

    async def test_fatal_modal_anonymous_network_raises_permission_error_immediately(self):
        """New 2: 'Disable VPN, proxy or Tor traffic' raises FreebuffPermissionError."""
        chunk = b"Freebuff detected anonymous network traffic. Disable VPN, proxy or Tor traffic and try again."
        with patch("os.read", return_value=chunk):
            self.worker._on_pty_readable()

        self.assertTrue(self.worker.ready_event.is_set())
        with self.assertRaises(FreebuffPermissionError) as caught:
            self.worker._check_interaction()
        self.assertEqual(caught.exception.error_code, "anonymous_network_blocked")

    async def test_fatal_modal_geoblock_raises_permission_error_immediately(self):
        """New 2: 'could not verify your location' raises country_blocked."""
        chunk = b"Freebuff could not verify your location. Check your VPN or proxy settings."
        with patch("os.read", return_value=chunk):
            self.worker._on_pty_readable()

        self.assertTrue(self.worker.ready_event.is_set())
        with self.assertRaises(FreebuffPermissionError) as caught:
            self.worker._check_interaction()
        self.assertEqual(caught.exception.error_code, "country_blocked")

    async def test_fatal_modal_out_of_hours_raises_payment_error_immediately(self):
        """New 2: 'Not enough hours' raises FreebuffPaymentError (402)."""
        chunk = b"Not enough hours: 0.0 available. See https://freebuff.com/plans or use /model."
        with patch("os.read", return_value=chunk):
            self.worker._on_pty_readable()

        self.assertTrue(self.worker.ready_event.is_set())
        with self.assertRaises(FreebuffPaymentError) as caught:
            self.worker._check_interaction()
        self.assertEqual(caught.exception.status_code, 402)
        self.assertEqual(caught.exception.error_code, "insufficient_quota")

    async def test_fatal_modal_session_limit_reached_raises_payment_error_immediately(self):
        """New 2: 'Session limit reached' raises FreebuffPaymentError (402)."""
        chunk = b"Session limit reached. See https://freebuff.com/plans"
        with patch("os.read", return_value=chunk):
            self.worker._on_pty_readable()

        self.assertTrue(self.worker.ready_event.is_set())
        with self.assertRaises(FreebuffPaymentError) as caught:
            self.worker._check_interaction()
        self.assertEqual(caught.exception.status_code, 402)
        self.assertEqual(caught.exception.error_code, "insufficient_quota")

    async def test_fatal_modal_ip_capped_raises_rate_limit_with_retry_after(self):
        """New 2: 'Too many Freebuff sessions on this network' raises FreebuffRateLimitError with 30s cooldown."""
        chunk = b"Too many Freebuff sessions on this network. Try again after another session finishes."
        with patch("os.read", return_value=chunk):
            self.worker._on_pty_readable()

        self.assertTrue(self.worker.ready_event.is_set())
        with self.assertRaises(FreebuffRateLimitError) as caught:
            self.worker._check_interaction()
        self.assertEqual(caught.exception.status_code, 429)
        self.assertEqual(caught.exception.error_code, "ip_capped")
        self.assertEqual(caught.exception.retry_after, 30.0)

    async def test_pool_select_worker_reports_payment_and_permission_errors(self):
        """New 2: Pool reports typed 402/403 when accounts have payment or permission errors."""
        pool = MultiAccountWorkerPool()

        # Mock global_account_manager
        acc1 = AccountState(AccountConfig(id="acc1", name="Acc 1", token="tok1", enabled=True))
        acc1.status = "payment_error"

        with patch("app.pool.global_account_manager.accounts", {"acc1": acc1}):
            with self.assertRaises(FreebuffPaymentError) as caught:
                pool.select_worker()
            self.assertEqual(caught.exception.status_code, 402)

        acc2 = AccountState(AccountConfig(id="acc2", name="Acc 2", token="tok2", enabled=True))
        acc2.status = "permission_error"

        with patch("app.pool.global_account_manager.accounts", {"acc2": acc2}):
            with self.assertRaises(FreebuffPermissionError) as caught:
                pool.select_worker()
            self.assertEqual(caught.exception.status_code, 403)

    async def test_takeover_cooldown_prevents_repeated_confirm(self):
        """Takeover auto-confirms once, but repeated prompts within cooldown abort."""
        written = []

        async def mock_write_all(fd, data):
            written.append((fd, data))

        with patch("app.worker.write_all", mock_write_all), \
             patch("app.worker.TAKEOVER_AUTO_CONFIRM", True), \
             patch("app.worker.ADMISSION_AUTO_CONFIRM", False), \
             patch("app.worker.TAKEOVER_COOLDOWN_SECONDS", 600), \
             patch("random.uniform", return_value=0.01):

            # Attempt 1: First takeover should confirm
            with patch("os.read", return_value=b"Do you want to take over the session? [Enter: confirm]"):
                self.worker._on_pty_readable()
            self.assertTrue(self.worker._takeover_scheduled)
            await self.worker._takeover_task
            self.assertEqual(written, [(999, b"\r")])

            # Attempt 2: Immediate repeated takeover within cooldown window
            written.clear()
            with patch("os.read", return_value=b"Do you want to take over the session? [Enter: confirm]"):
                self.worker._on_pty_readable()
            self.assertFalse(self.worker._takeover_scheduled)
            self.assertEqual(len(written), 0)

            # _check_interaction should now raise FreebuffInteractionRequiredError
            with self.assertRaises(FreebuffInteractionRequiredError) as caught:
                self.worker._check_interaction()
            self.assertIn("takeover within 600s cooldown", str(caught.exception))
            self.assertEqual(caught.exception.error_code, "takeover_cooldown")

    async def test_admission_hourly_limit_prevents_spend_draining(self):
        """Admission auto-confirms up to MAX_HOURLY_ADMISSION, then pauses to protect quota."""
        written = []

        async def mock_write_all(fd, data):
            written.append((fd, data))

        with patch("app.worker.write_all", mock_write_all), \
             patch("app.worker.ADMISSION_AUTO_CONFIRM", True), \
             patch("app.worker.TAKEOVER_AUTO_CONFIRM", False), \
             patch("app.worker.MAX_HOURLY_ADMISSION", 2), \
             patch("random.uniform", return_value=0.01):

            # Confirm 1
            with patch("os.read", return_value=b"Enter: confirm and send"):
                self.worker._on_pty_readable()
            await self.worker._admission_task

            # Confirm 2 (hits max 2)
            with patch("os.read", return_value=b"Enter: confirm and send"):
                self.worker._on_pty_readable()
            await self.worker._admission_task

            self.assertEqual(len(written), 2)

            # Confirm 3 (exceeds hourly limit)
            written.clear()
            with patch("os.read", return_value=b"Enter: confirm and send"):
                self.worker._on_pty_readable()
            self.assertFalse(self.worker._admission_scheduled)
            self.assertEqual(len(written), 0)

            # _check_interaction should raise rate limit error
            with self.assertRaises(FreebuffInteractionRequiredError) as caught:
                self.worker._check_interaction()
            self.assertIn("Hourly model admission/spend confirmation limit", str(caught.exception))
            self.assertEqual(caught.exception.error_code, "admission_rate_limited")

    async def test_conversational_takeover_does_not_trigger_modal_handler(self):
        """Conversational mentions of 'takeover' or 'take over' in AI answers/prompts must not trigger modal."""
        written = []

        async def mock_write_all(fd, data):
            written.append((fd, data))

        with patch("app.worker.write_all", mock_write_all), \
             patch("app.worker.TAKEOVER_AUTO_CONFIRM", True), \
             patch("app.worker.TAKEOVER_COOLDOWN_SECONDS", 600):

            # Simulate conversational output discussing subdomain takeover
            self.worker._raw_buffer.extend(b"Subdomain takeover happens when DNS points to a deleted resource.\nI will take over the task.\n")
            self.worker._on_pty_readable()
            self.assertFalse(self.worker._takeover_scheduled)
            self.assertEqual(len(written), 0)

            # _check_interaction should NOT raise FreebuffInteractionRequiredError
            self.worker._check_interaction()


if __name__ == "__main__":
    unittest.main()
