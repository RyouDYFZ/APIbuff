"""Offline source/scope tests: fake credentials, device keys, and HTTP responses."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

from app import auth_probe, main
from app.catalog import API_HOST, FreebuffCatalogService, b64u_decode, b64u_encode
from app.models import PricingResponse
from app.routes import health, models
from tests.test_chat_protocol import ASGIExchange

CATALOG = {"fetchId": "fetch-test", "rows": [{
    "key": "model-test", "handle": "provider/test-model", "displayName": "Test Model", "defaultPrice": 5,
}]}
SESSION = {
    "accessTier": "test_tier", "countryCode": "GB", "countryVerified": False,
    "freebucks": {"balance": 12, "daily": {"limit": 20, "remaining": 4}, "wallet": {"balance": 8}, "prices": {"model-test": 7}},
}


def response(status=200, body=None):
    return SimpleNamespace(status_code=status, json=lambda: deepcopy(body))


class CatalogTruthTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        self.env = patch.dict("os.environ", {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.keychain = patch("subprocess.check_output", side_effect=AssertionError("keychain must not be queried"))
        self.keychain_mock = self.keychain.start()
        self.addCleanup(self.keychain.stop)
        self.network = patch("app.catalog.requests.get", side_effect=AssertionError("unexpected network request"))
        self.get = self.network.start()
        self.addCleanup(self.network.stop)
        self.private = Ed25519PrivateKey.generate()
        self.private_b64 = b64u_encode(self.private.private_bytes(Encoding.DER, PrivateFormat.PKCS8, NoEncryption()))

    def credentials(self, token="token-a", account_id="account-a", *, registrations=None):
        profile = {"authToken": token, "email": "test@example.invalid"}
        if account_id is not None:
            profile["id"] = account_id
        (self.path / "credentials.json").write_text(json.dumps({"default": profile}))
        scope = f"{API_HOST} user:{account_id}" if account_id else f"{API_HOST} token:{hashlib.sha256(token.encode()).hexdigest()[:32]}"
        if registrations is None:
            registrations = {scope: "correct-key"}
        (self.path / "device-key.json").write_text(json.dumps({"privateKey": self.private_b64, "registrations": registrations}))
        return scope

    def successful_refreshes(self):
        self.get.side_effect = lambda url, **kwargs: response(body=CATALOG if url.endswith("/models") else SESSION)

    def test_signs_only_exact_user_and_host_registration(self):
        scope = f"{API_HOST} user:account-a"
        self.credentials(registrations={"https://other.example user:account-a": "wrong-host", f"{API_HOST} user:other": "wrong-user", scope: "correct-key"})
        self.successful_refreshes()
        service = FreebuffCatalogService(str(self.path), api_host=API_HOST + "/")
        service.refresh()
        self.assertEqual(self.get.call_count, 2)
        for call in self.get.call_args_list:
            url, headers = call.args[0], call.kwargs["headers"]
            self.assertEqual(headers["x-freebuff-device-key"], "correct-key")
            path = url.removeprefix(API_HOST)
            fetch = "fetch-test" if path.endswith("/session") else ""
            signed = f"freebuff-device-v1\nGET\n{path}\n{headers['x-freebuff-device-ts']}\n{hashlib.sha256(b'').hexdigest()}\n{fetch}".encode()
            self.private.public_key().verify(b64u_decode(headers["x-freebuff-device-sig"]), signed)
        self.keychain_mock.assert_not_called()

    def test_environment_token_never_borrows_profile_account_id(self):
        token = "environment-token"
        scope = f"{API_HOST} token:{hashlib.sha256(token.encode()).hexdigest()[:32]}"
        self.credentials(registrations={f"{API_HOST} user:account-a": "wrong-user-key", scope: "environment-key"})
        self.successful_refreshes()
        with patch.dict("os.environ", {"CODEBUFF_API_KEY": token}):
            service = FreebuffCatalogService(str(self.path))
            service.refresh()
        self.assertEqual(service.key_id, "environment-key")
        self.assertEqual(service._scope, scope)

    def test_token_scope_is_used_without_profile_id(self):
        scope = self.credentials(account_id=None)
        self.successful_refreshes()
        service = FreebuffCatalogService(str(self.path))
        service.refresh()
        self.assertEqual(service._scope, scope)
        self.assertEqual(service.key_id, "correct-key")

    def test_unregistered_scope_does_not_send_or_register_anything(self):
        self.credentials(registrations={f"{API_HOST} user:another-account": "wrong-key"})
        service = FreebuffCatalogService(str(self.path))
        result = service.get_pricing_overview()
        self.assertIsNone(service.key_id)
        self.assertIsNone(service.priv_key)
        self.assertIsNone(result["account"])
        self.assertIsNone(result["balance"])
        self.assertEqual(result["metadata"]["device_registration"], "unregistered")
        self.get.assert_not_called()

    def test_each_refresh_reloads_and_clears_deleted_key_even_within_ttl(self):
        self.credentials()
        self.successful_refreshes()
        service = FreebuffCatalogService(str(self.path))
        self.assertEqual(service.get_pricing_overview()["balance"]["total_freebucks"], 12)
        (self.path / "device-key.json").unlink()
        result = service.get_pricing_overview()
        self.assertIsNone(service.key_id)
        self.assertIsNone(service.priv_key)
        self.assertIsNone(result["account"])
        self.assertIsNone(result["balance"])
        self.assertEqual(self.get.call_count, 2)

    def test_token_rotation_never_keeps_previous_account_balance(self):
        self.credentials()
        self.successful_refreshes()
        service = FreebuffCatalogService(str(self.path))
        self.assertEqual(service.get_pricing_overview()["balance"]["total_freebucks"], 12)
        self.credentials(token="token-b", account_id="account-b", registrations={f"{API_HOST} user:account-a": "old-key"})
        result = service.get_pricing_overview()
        self.assertIsNone(result["account"])
        self.assertIsNone(result["balance"])
        self.assertEqual(self.get.call_count, 2)

    def test_401_403_network_and_malformed_session_clear_cached_identity(self):
        self.credentials()
        for failure in (response(401), response(403), OSError("offline"), response(200, {"freebucks": []})):
            with self.subTest(failure=failure):
                self.successful_refreshes()
                service = FreebuffCatalogService(str(self.path))
                self.assertIsNotNone(service.get_pricing_overview()["balance"])
                self.get.side_effect = [response(body=CATALOG), failure]
                service.refresh(force=True)
                result = service.get_pricing_overview()
                self.assertIsNone(result["account"])
                self.assertIsNone(result["balance"])
                self.assertEqual(result["balance_status"], "unknown")
                self.assertIsNotNone(result["error"])

    def test_catalog_failure_does_not_probe_session_after_401(self):
        self.credentials()
        self.get.side_effect = [response(401)]
        service = FreebuffCatalogService(str(self.path))
        result = service.get_pricing_overview()
        self.assertEqual(self.get.call_count, 1)
        self.assertIsNone(result["account"])
        self.assertEqual(result["source"], "local_fallback")

    def test_fallback_has_no_fabricated_account_balance_or_free_price(self):
        service = FreebuffCatalogService(str(self.path))
        result = service.get_pricing_overview()
        parsed = PricingResponse.model_validate(result)
        self.assertIsNone(parsed.account)
        self.assertIsNone(parsed.balance)
        self.assertEqual(result["metadata"]["scope"], "default_credentials_only")
        self.assertFalse(result["metadata"]["includes_all_accounts"])
        for model in result["models"]:
            self.assertEqual(model["source"], "local_fallback")
            self.assertIsNone(model["pricing"]["freebucks_per_hour"])
            self.assertIsNone(model["pricing"]["is_free"])
            self.assertEqual(model["capabilities"]["access"], "unknown")
        self.get.assert_not_called()

    def test_missing_session_fields_remain_null_including_zero_balances(self):
        self.credentials()
        session = {"freebucks": {"balance": 0}, "countryVerified": False}
        self.get.side_effect = [response(body=CATALOG), response(body=session)]
        result = FreebuffCatalogService(str(self.path)).get_pricing_overview()
        self.assertFalse(result["account"]["country_verified"])
        self.assertIsNone(result["account"]["country_code"])
        self.assertEqual(result["balance"]["total_freebucks"], 0)
        self.assertIsNone(result["balance"]["daily"]["limit_freebucks"])
        self.assertIsNone(result["balance"]["wallet"]["monthly_bonus"])
        self.assertIsNone(result["models"][0]["capabilities"]["plan_required"])
        PricingResponse.model_validate(result)

    def test_plan_eligibility_requires_an_explicit_projection_or_locked_row(self):
        self.credentials()
        for session, access, expected in (
            ({"countryCode": "GB"}, "open", None),
            ({"freebucks": {}}, "open", None),
            ({"freebucks": {"planRequiredModelIds": []}}, "open", False),
            ({"freebucks": {"planRequiredModelIds": ["model-test"]}}, "open", True),
            ({"countryCode": "GB"}, "locked", True),
        ):
            with self.subTest(session=session, access=access):
                catalog = deepcopy(CATALOG)
                catalog["rows"][0]["access"] = access
                self.get.side_effect = [response(body=catalog), response(body=session)]
                result = FreebuffCatalogService(str(self.path)).get_pricing_overview()
                self.assertIs(result["models"][0]["capabilities"]["plan_required"], expected)

    def test_finite_fractional_balances_prices_and_refunds_are_preserved(self):
        self.credentials()
        session = {
            "countryCode": "GB",
            "freebucks": {
                "balance": 12.75, "daily": {"limit": 20.5, "spent": 8.25, "remaining": 12.25},
                "wallet": {"balance": 0.5, "monthlyBonus": 1.25},
                "prices": {"model-test": 0.75},
                "lastRefund": {"id": "refund-test", "daily": 1.5, "wallet": 0.25},
            },
        }
        self.get.side_effect = [response(body=CATALOG), response(body=session)]
        result = PricingResponse.model_validate(FreebuffCatalogService(str(self.path)).get_pricing_overview())
        self.assertEqual(result.balance.total_freebucks, 12.75)
        self.assertEqual(result.balance.daily.remaining_freebucks, 12.25)
        self.assertEqual(result.balance.wallet.monthly_bonus, 1.25)
        self.assertEqual(result.models[0].pricing.freebucks_per_hour, 0.75)
        self.assertEqual(result.balance.last_refund["daily"], 1.5)
        json.dumps(result.model_dump(), allow_nan=False)

    def test_invalid_session_values_clear_cached_balances_before_serialization(self):
        self.credentials()
        patches = [
            {"balance": float("nan")}, {"balance": float("inf")}, {"balance": True},
            {"daily": {"remaining": "25"}}, {"wallet": {"balance": float("-inf")}},
            {"prices": {"model-test": float("nan")}},
            {"priceNotices": {"model-test": {"unexpected": "object"}}},
            {"offPeak": {"model-test": ["unexpected"]}},
            {"daily": {"resetAt": {"unexpected": "object"}}},
            {"lastRefund": "bad-shape"}, {"lastRefund": {}},
            {"lastRefund": {"id": "refund", "daily": float("nan"), "wallet": 0}},
            {"lastRefund": {"id": "refund", "daily": 1, "wallet": 0, "extra": float("inf")}},
        ]
        sessions = [dict(SESSION, freebucks=dict(SESSION["freebucks"], **patch)) for patch in patches]
        sessions.extend((dict(SESSION, subscription={"tiers": ["bad-shape"]}),
                         dict(SESSION, subscription={"tiers": [{"extra": float("nan")}]})))
        for session in sessions:
            with self.subTest(session=session):
                self.successful_refreshes()
                service = FreebuffCatalogService(str(self.path))
                self.assertIsNotNone(service.get_pricing_overview()["balance"])
                self.get.side_effect = [response(body=CATALOG), response(body=session)]
                service.refresh(force=True)
                result = service.get_pricing_overview()
                self.assertIsNone(result["account"])
                self.assertIsNone(result["balance"])
                self.assertEqual(result["error"]["code"], "invalid_session_response")
                parsed = PricingResponse.model_validate(result)
                json.dumps(parsed.model_dump(), allow_nan=False)

    def test_invalid_catalog_values_fall_back_before_serialization(self):
        self.credentials()
        for patch in (
            {"defaultPrice": float("nan")}, {"defaultPrice": float("inf")},
            {"defaultPrice": True}, {"contextWindow": 1.5}, {"contextWindow": "bad"},
            {"tagline": {}}, {"badges": None}, {"efforts": [False]},
        ):
            with self.subTest(patch=patch):
                catalog = deepcopy(CATALOG)
                catalog["rows"][0].update(patch)
                self.get.side_effect = [response(body=catalog)]
                result = FreebuffCatalogService(str(self.path)).get_pricing_overview()
                self.assertEqual(result["error"]["code"], "invalid_catalog_response")
                self.assertEqual(result["source"], "local_fallback")
                parsed = PricingResponse.model_validate(result)
                json.dumps(parsed.model_dump(), allow_nan=False)

    def test_current_default_config_directory_is_resolved_again_on_refresh(self):
        self.credentials()
        self.successful_refreshes()
        with patch("app.auth_probe.default_config_dir", return_value=str(self.path)) as directory:
            service = FreebuffCatalogService()
            self.assertIsNotNone(service.get_pricing_overview()["balance"])
            directory.return_value = str(self.path / "different-profile")
            result = service.get_pricing_overview()
        self.assertIsNone(result["balance"])
        self.assertEqual(self.get.call_count, 2)

    def test_keychain_pointer_is_not_replaced_by_an_unscoped_secret(self):
        (self.path / "credentials.json").write_text(json.dumps({"default": {"tokenStore": "keychain", "id": "account-a"}}))
        service = FreebuffCatalogService(str(self.path))
        result = service.get_pricing_overview()
        self.assertIsNone(service.token)
        self.assertIsNone(result["balance"])
        self.get.assert_not_called()
        self.keychain_mock.assert_not_called()

    def test_concurrent_readers_refresh_once_and_get_independent_snapshots(self):
        self.credentials()
        def get(url, **kwargs):
            time.sleep(0.01)
            return response(body=CATALOG if url.endswith("/models") else SESSION)
        self.get.side_effect = get
        service = FreebuffCatalogService(str(self.path))
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: service.get_pricing_overview(), range(16)))
        self.assertEqual(self.get.call_count, 2)
        self.assertTrue(all(result["balance"]["total_freebucks"] == 12 for result in results))
        results[0]["models"][0]["pricing"]["freebucks_per_hour"] = 999
        self.assertEqual(service.get_all_models()[0]["pricing"]["freebucks_per_hour"], 7)

    def test_model_aliases_are_exact_and_do_not_cross_model_generations(self):
        service = FreebuffCatalogService(str(self.path))
        self.assertIsNotNone(service.resolve_model("openai/gpt-6-luna"))
        self.assertIsNotNone(service.resolve_model("GPT-6 Luna".lower().replace(" ", "-")))
        for model in ("gpt-4o", "gpt-6-luna-something", "prefix-gpt-6-luna", "gpt-6-lun", ""):
            self.assertIsNone(service.resolve_model(model))


class AuthTruthTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        self.env = patch.dict("os.environ", {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.config = patch.object(auth_probe, "default_config_dir", return_value=str(self.path))
        self.config_mock = self.config.start()
        self.addCleanup(self.config.stop)
        self.get_patch = patch.object(auth_probe.requests, "get")
        self.get = self.get_patch.start()
        self.addCleanup(self.get_patch.stop)
        auth_probe._CACHE_RESULT, auth_probe._CACHE_SCOPE, auth_probe._CACHE_TIMESTAMP = None, None, 0

    def test_auth_cache_is_scoped_to_both_token_and_directory(self):
        self.get.return_value = response(body={"id": "test-id", "email": "test@example.invalid"})
        with patch.dict("os.environ", {"CODEBUFF_API_KEY": "token-a"}):
            auth_probe.verify_token()
            auth_probe.verify_token()
        self.assertEqual(self.get.call_count, 1)
        with patch.dict("os.environ", {"CODEBUFF_API_KEY": "token-b"}):
            auth_probe.verify_token()
            self.config_mock.return_value = str(self.path / "another-profile")
            auth_probe.verify_token()
        self.assertEqual(self.get.call_count, 3)

    def test_network_403_and_unknown_endpoint_are_not_token_expiry(self):
        with patch.dict("os.environ", {"CODEBUFF_API_KEY": "token-a"}):
            for outcome in (OSError("offline"), response(403), response(404), response(200, {})):
                with self.subTest(outcome=outcome):
                    self.get.side_effect = outcome if isinstance(outcome, Exception) else None
                    self.get.return_value = outcome
                    result = auth_probe.verify_token(force=True)
                    self.assertIsNone(result["valid"])
                    self.assertNotEqual(result["status"], "expired")
                    self.assertIsNone(result["login_command"])
            self.get.side_effect = None
            self.get.return_value = response(401)
            self.assertFalse(auth_probe.verify_token(force=True)["valid"])

    def test_keychain_pointer_never_runs_security_command(self):
        (self.path / "credentials.json").write_text(json.dumps({"default": {"tokenStore": "keychain"}}))
        with patch("subprocess.check_output", side_effect=AssertionError("forbidden")) as command:
            result = auth_probe.verify_token(force=True)
        self.assertEqual(result["source"], "keychain_unavailable")
        self.assertIsNone(result["valid"])
        self.get.assert_not_called()
        command.assert_not_called()


class PublicAndThreadBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_health_reads_only_local_worker_count(self):
        with patch.object(main, "API_KEY", "protected"), patch.object(health.global_pool, "workers", {"private-account": SimpleNamespace(is_running=True)}), patch.object(health, "verify_token", side_effect=AssertionError("public probe")) as verify, patch.object(auth_probe, "read_auth_context", side_effect=AssertionError("public credentials")) as read, patch("requests.get", side_effect=AssertionError("public network")) as network, patch("subprocess.check_output", side_effect=AssertionError("public keychain")) as keychain:
            result = await ASGIExchange(method="GET", path="/health").run()
        self.assertEqual(result.status, 200)
        self.assertEqual(set(result.json), {"status", "worker_count", "uptime_seconds"})
        self.assertEqual(result.json["worker_count"], 1)
        for mock in (verify, read, network, keychain):
            mock.assert_not_called()

    async def test_auth_diagnostic_requires_bearer_and_runs_in_thread(self):
        loop_thread = threading.get_ident()
        threads = []
        def verify(**kwargs):
            threads.append(threading.get_ident())
            return {"status": "unknown", "valid": None}
        with patch.object(main, "API_KEY", "protected"), patch.object(health, "verify_token", side_effect=verify) as mock:
            rejected = await ASGIExchange(method="GET", path="/v1/auth").run()
            self.assertEqual(rejected.status, 401)
            mock.assert_not_called()
            accepted = await ASGIExchange(method="GET", path="/v1/auth", headers={"Authorization": "Bearer protected"}).run()
        self.assertEqual(accepted.status, 200)
        self.assertEqual(len(threads), 1)
        self.assertNotEqual(threads[0], loop_thread)

    async def test_catalog_routes_each_make_one_service_call_off_event_loop(self):
        loop_thread = threading.get_ident()
        cases = [
            ("/v1/models", "get_catalog_overview", {"object": "list", "data": [], "source": "local_fallback"}),
            ("/v1/models/test-model", "get_model", {"id": "test-model", "source": "local_fallback"}),
            ("/v1/pricing", "get_pricing_overview", {"models": [], "models_count": 0}),
        ]
        with patch.object(main, "API_KEY", ""):
            for path, method, payload in cases:
                with self.subTest(path=path):
                    threads = []
                    def call(*args):
                        threads.append(threading.get_ident())
                        return payload
                    with patch.object(models.catalog_service, method, side_effect=call) as service:
                        result = await ASGIExchange(method="GET", path=path).run()
                    self.assertEqual(result.status, 200)
                    service.assert_called_once()
                    self.assertNotEqual(threads[0], loop_thread)


if __name__ == "__main__":
    unittest.main()
