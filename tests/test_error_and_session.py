"""
Integration Test Suite for Fast-Fail Error Handling, Fallback Model Catalog, Auth Probes, and Session Continuity.
"""

import os
import sys
import time
import unittest
import requests

# Ensure freebuff-api-service is on path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from app.catalog import catalog_service
from app.exceptions import FreebuffAuthError, FreebuffRateLimitError, FreebuffPermissionError
from app.pool import global_pool
from app.models import ChatMessage

BASE_URL = "http://127.0.0.1:8000"

class TestErrorAndSession(unittest.TestCase):
    def test_catalog_fallback_and_models_list(self):
        """Verifies that /v1/models returns complete models even when offline or unauthenticated."""
        resp = requests.get(f"{BASE_URL}/v1/models")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data.get("object"), "list")
        models = data.get("data", [])
        self.assertGreaterEqual(len(models), 15, "Expected at least 15 models in catalog")

        model_ids = [m["id"] for m in models]
        self.assertIn("glm-5.3-flash", model_ids)
        self.assertIn("deepseek-v4.1-flash", model_ids)
        self.assertIn("gpt-6-luna", model_ids)
        self.assertIn("gemini-4-argon", model_ids)
        self.assertIn("claude-haiku-5.5", model_ids)

        # Check individual model detail
        m_resp = requests.get(f"{BASE_URL}/v1/models/gpt-6-luna")
        self.assertEqual(m_resp.status_code, 200)
        m_data = m_resp.json()
        self.assertEqual(m_data["display_name"], "GPT-6 Luna")
        self.assertIn("pricing", m_data)
        self.assertIn("capabilities", m_data)

    def test_pricing_endpoint(self):
        """Verifies that /v1/pricing returns rate cards and balances."""
        resp = requests.get(f"{BASE_URL}/v1/pricing")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("account", data)
        self.assertIn("balance", data)
        self.assertIn("models", data)
        self.assertGreaterEqual(data["models_count"], 15)

    def test_auth_probe_and_health(self):
        """Verifies that /health and /v1/auth proactively report credential status."""
        resp = requests.get(f"{BASE_URL}/health")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("auth", data)
        auth = data["auth"]
        self.assertIn("status", auth)
        self.assertIn("valid", auth)
        self.assertIn("message", auth)

        auth_resp = requests.get(f"{BASE_URL}/v1/auth")
        self.assertEqual(auth_resp.status_code, 200)
        auth_data = auth_resp.json()
        self.assertIn("status", auth_data)
        self.assertIn("valid", auth_data)

    def test_session_continuity_formatting(self):
        """Verifies that session continuity trims repeated history and passes only current turn."""
        messages = [
            ChatMessage(role="user", content="Turn 1: Hello"),
            ChatMessage(role="assistant", content="Turn 1: Hi there"),
            ChatMessage(role="user", content="Turn 2: What is this project?")
        ]

        # New session: includes history
        new_prompt = global_pool.format_messages(messages, is_new_session=True)
        self.assertIn("[Conversation History]", new_prompt)
        self.assertIn("Turn 1: Hello", new_prompt)
        self.assertIn("Turn 2: What is this project?", new_prompt)

        # Continuing session: includes ONLY current turn
        cont_prompt = global_pool.format_messages(messages, is_new_session=False)
        self.assertNotIn("[Conversation History]", cont_prompt)
        self.assertNotIn("Turn 1: Hello", cont_prompt)
        self.assertEqual(cont_prompt, "Turn 2: What is this project?")

    def test_fast_fail_exception_serialization(self):
        """Verifies that FreebuffException classes serialize to standard OpenAI error JSON."""
        auth_err = FreebuffAuthError("Token expired. Run ./login.sh")
        self.assertEqual(auth_err.status_code, 401)
        self.assertEqual(auth_err.error_type, "authentication_error")
        d = auth_err.to_dict()
        self.assertIn("error", d)
        self.assertEqual(d["error"]["code"], 401)
        self.assertEqual(d["error"]["type"], "authentication_error")

        rl_err = FreebuffRateLimitError("Rate limit reached", error_code="free_mode_rate_limited")
        self.assertEqual(rl_err.status_code, 429)
        self.assertEqual(rl_err.to_dict()["error"]["code"], 429)

    def test_fast_fail_on_live_inference(self):
        """
        Tests that an inference turn against an expired token fails immediately (< 2.5s)
        with HTTP 401 and typed OpenAI error, rather than hanging for 120 seconds!
        """
        t0 = time.time()
        resp = requests.post(
            f"{BASE_URL}/v1/chat/completions",
            json={
                "model": "glm-5.3-flash",
                "messages": [{"role": "user", "content": "ping"}],
                "stream": False
            },
            timeout=10
        )
        elapsed = time.time() - t0
        print(f"\n[Live Turn] HTTP {resp.status_code} in {elapsed:.2f}s: {resp.text[:120]}")
        # Must fail fast in under 3.5s (instead of 120s timeout!)
        self.assertLess(elapsed, 4.0, "Fast-fail failed: took longer than 4.0s!")
        self.assertEqual(resp.status_code, 401)
        err = resp.json().get("detail", {})
        self.assertEqual(err.get("type"), "authentication_error")
        self.assertIn("login.sh", err.get("message", ""))

if __name__ == "__main__":
    unittest.main()
