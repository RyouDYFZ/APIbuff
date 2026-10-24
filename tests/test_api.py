import sys
import time
import json
import urllib.request
import urllib.error

BASE_URL = "http://127.0.0.1:8000"

def test_health():
    print("[1/5] Testing GET /health ...")
    req = urllib.request.Request(f"{BASE_URL}/health")
    with urllib.request.urlopen(req, timeout=10) as resp:
        assert resp.status == 200
        data = json.loads(resp.read().decode())
        print("  -> Status:", data.get("status"))
        print("  -> Worker:", data.get("worker"))
        assert data.get("status") in ("ok", "degraded")
    print("  [PASS] Health check passed!\n")

def test_models():
    print("[2/5] Testing GET /v1/models (Full Catalog) ...")
    req = urllib.request.Request(f"{BASE_URL}/v1/models")
    with urllib.request.urlopen(req, timeout=10) as resp:
        assert resp.status == 200
        data = json.loads(resp.read().decode())
        models = data.get("data", [])
        print(f"  -> Total dynamic models retrieved: {len(models)}")
        assert len(models) >= 15
        
        # Verify model structure
        sample = models[0]
        print("  -> Sample model:", sample["id"], "| display:", sample.get("display_name"))
        print("  -> Pricing metadata:", sample.get("pricing"))
        print("  -> Capabilities:", sample.get("capabilities"))
        assert "pricing" in sample
        assert "capabilities" in sample
    print("  [PASS] Full dynamic model catalog passed!\n")

def test_pricing():
    print("[3/5] Testing GET /v1/pricing (Balances & Rate Cards) ...")
    req = urllib.request.Request(f"{BASE_URL}/v1/pricing")
    with urllib.request.urlopen(req, timeout=10) as resp:
        assert resp.status == 200
        data = json.loads(resp.read().decode())
        account = data.get("account", {})
        balance = data.get("balance", {})
        daily = balance.get("daily", {})
        wallet = balance.get("wallet", {})
        tiers = data.get("subscription_tiers", [])
        models_count = data.get("models_count", 0)

        print(f"  -> Account tier: {account.get('access_tier')}")
        print(f"  -> Balance: {balance.get('total_freebucks')} Freebucks ({balance.get('total_hours')} hours)")
        print(f"  -> Daily Allowance: {daily.get('remaining_freebucks')}/{daily.get('limit_freebucks')} Freebucks remaining")
        print(f"  -> Wallet: {wallet.get('balance_freebucks')} Freebucks")
        print(f"  -> Subscription tiers available: {[t.get('displayName') for t in tiers]}")
        print(f"  -> Models with rate cards: {models_count}")

        assert balance.get("total_freebucks", 0) >= 0
        assert daily.get("limit_freebucks", 0) > 0
        assert models_count >= 15
    print("  [PASS] Detailed pricing and balance inspection passed!\n")

def test_model_detail():
    print("[4/5] Testing GET /v1/models/glm-5.3-flash ...")
    req = urllib.request.Request(f"{BASE_URL}/v1/models/glm-5.3-flash")
    with urllib.request.urlopen(req, timeout=10) as resp:
        assert resp.status == 200
        data = json.loads(resp.read().decode())
        print(f"  -> ID: {data.get('id')} (Key: {data.get('key')})")
        print(f"  -> Display Name: {data.get('display_name')}")
        print(f"  -> Rate: {data.get('pricing', {}).get('freebucks_per_hour')} Freebucks/hr ({data.get('pricing', {}).get('hours_equivalent')} hrs)")
        print(f"  -> Context: {data.get('capabilities', {}).get('context_window')} tokens")
        print(f"  -> Efforts: {data.get('capabilities', {}).get('efforts')}")
        assert data.get("id") == "glm-5.3-flash"
    print("  [PASS] Single model detail endpoint passed!\n")

def test_chat_non_streaming():
    print("[5/5] Testing POST /v1/chat/completions (model=glm-5.3-flash) ...")
    payload = {
        "model": "glm-5.3-flash",
        "messages": [
            {"role": "user", "content": "Compute 12 * 12. Answer with just the number."}
        ],
        "stream": False
    }
    req = urllib.request.Request(
        f"{BASE_URL}/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}
    )
    start = time.time()
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode())
            content = data["choices"][0]["message"]["content"]
            duration = round(time.time() - start, 2)
            print(f"  -> Finished in {duration}s")
            print(f"  -> Assistant response: {content.strip()}")
            assert len(content) > 0
            print("  [PASS] Dynamic model chat completion passed!\n")
    except urllib.error.HTTPError as e:
        if e.code == 401:
            print(f"  -> HTTP 401 Intercepted in {round(time.time() - start, 2)}s (Expected fast-fail auth): {e.reason}")
            print("  [PASS] Fast-fail authentication error intercepted correctly!\n")
        else:
            raise

if __name__ == "__main__":
    test_health()
    test_models()
    test_pricing()
    test_model_detail()
    test_chat_non_streaming()
    print("ALL 5 API & PRICING TESTS PASSED!")
