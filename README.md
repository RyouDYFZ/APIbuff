# Freebuff OpenAI-Compatible API Gateway

**English** | [简体中文](README.zh-CN.md)

An OpenAI-compatible text API proxy for **Freebuff CLI** (`npm install -g freebuff`), architected with reference to conversion engines like [sub2api](https://github.com/Wei-Shaw/sub2api).

---

## 🌟 Architecture & Safety Compliance

The extracted Freebuff client reports environment and input-profile information. Local PTY and typing measurements do not establish server acceptance, permission, or enforcement thresholds.

`freebuff-api-service` implements an **authentic PTY-driven worker architecture**:

1. **Isolated Pseudo-Terminal (PTY)**: Drives the authentic `freebuff` binary inside virtual master/slave PTY sessions with real TTY terminal descriptors.
2. **Instant OSC 10/11 Color Detection Query Replies**: Automatically intercepts and answers OpenTUI terminal theme queries (`\x1b]11;rgb:0000/0000/0000\x07` / `\x1b]10;rgb:ffff/ffff/ffff\x07`) across fragmented PTY chunks, eliminating terminal detection timeouts and shaving ~1.0 second off CLI startup on every worker turn.
3. **Model Admission & Spend Confirmation Auto-Handling**: Detects interactive TUI admission confirmation modals (`Enter: confirm and send`) and bounded retry prompts (`Enter: retry`), simulating human cognitive reading delays before safely auto-confirming.
4. **Sub-Second Fast-Fail for Fatal TUI Modals**: Continuously scans the PTY stream to intercept fatal account, network, and quota modals (suspended accounts, anonymous network blocks, geoblocking, exhausted hours, and IP concurrency caps) without waiting for 120-second request timeouts.
5. **Correlated Public Responses**: Binds output to a new user message in `chat-messages.json`, reads public response blocks, and requires a successful completion record. Errors and timeouts never fall back to earlier answers or terminal screen text.
6. **Ultra-Long Prompt Attachment Transport**: Prompts exceeding the CLI's 2000 UTF-16 character threshold are automatically packaged into native CLI text attachments (`textAttachments`), seamlessly supporting 32k–128k character context windows without truncation.
7. **Worker Lifecycle & Process Isolation**: Each PTY worker runs under a supervisor that terminates its process group when the gateway closes its lifetime pipe, including after an abrupt gateway exit or SIGKILL.
8. **Security, Privacy & Anti-Fingerprinting Guardrails**: Built-in macOS hardware fingerprint virtualization, dual-platform process ancestry normalization (`p=shell;g=terminal`), PostHog telemetry opt-out, Git remote URL privacy protection, and proxy loopback virtualization.
9. **Model Catalog & Pricing**: Cryptographic Ed25519 device-signed catalog queries expose 57+ models, live pricing rates (Freebucks/hour), peak/off-peak schedules, and remaining allowances. Missing credentials produce labelled unknown values.
10. **Exclusive Worker Turns**: One lease covers model/workspace changes and generation. Each request starts a new CLI chat with the supplied conversation history.

```
┌─────────────────────────────────────────────────────────────┐
│ Clients (Cursor, Cline, Roo Code, NextChat, OpenAI SDK, curl)│
└──────────────────────────────┬──────────────────────────────┘
                               │ HTTP / SSE (/v1/chat/completions, /v1/pricing)
┌──────────────────────────────▼──────────────────────────────┐
│                  Freebuff API Service (FastAPI)              │
│  - OpenAI Adapter (/v1/models, /v1/chat/completions)        │
│  - Dynamic Catalog & Pricing Engine (Ed25519 Signed)        │
│  - Worker Pool & Automatic Model Router                      │
│  - Real-Time PTY OSC 10/11 Color Stream Responder           │
│  - Sub-Second Fatal Modal & Admission Interceptor           │
│  - Ultra-Long Prompt Text Attachment Packager                │
│  - File Descriptor Elevation & Resource Lifecycle           │
└──────────────────────────────┬──────────────────────────────┘
                               │ PTY IPC (master / slave)
┌──────────────────────────────▼──────────────────────────────┐
│          Freebuff CLI Worker Supervisor & Runtime           │
│  - Dual-Platform Process Ancestry (p=shell;g=terminal)      │
│  - macOS Hardware Fingerprint Virtualization (UUID/Serial)   │
│  - PostHog Telemetry Opt-Out & Private CLI Integrity Build   │
│  - Git Remote URL Privacy Shield (fake_bin/git)             │
│  - Proxy Loopback Virtualization (px=remote)                │
│  - Real-time Keystroke Profile Telemetry & Jitter           │
└──────────────────────────────┬──────────────────────────────┘
                               │ HTTPS / TLS (Sing-box Outbound)
┌──────────────────────────────▼──────────────────────────────┐
│                      Codebuff Backend                       │
│  (57+ models: MiMo 2.6, GLM 5.3, DeepSeek V4.1, GPT-6, etc.) │
└─────────────────────────────────────────────────────────────┘
```

---

## 🚀 Quick Start

### 1. Requirements
- macOS or Linux

- Python 3.11+ (the runtime check refuses to start on older interpreters)

- Verified Private Freebuff CLI Build (`scripts/build_private_cli.py`)

  > [!NOTE]
  > Workers fail closed on the official CLI, whose compiled PostHog telemetry ignores every opt-out switch.

- Bun on `PATH` (`npm install -g bun@1.3`, for building the private CLI)

### 2. Install Dependencies
```bash
git clone https://github.com/RyouDYFZ/APIbuff.git
cd APIbuff
pip install -r requirements.txt
# or pin the exact verified set:
pip install -r requirements.lock
```

### 3. Build Private Verified CLI (Required)
Download the pinned official 0.2.34 release archive, verify its SHA-256, extract it, and rebuild it with the PostHog opt-out embedded:
```bash
python3 scripts/build_private_cli.py --fetch
```
This generates `.freebuff/private-cli/freebuff` with its `freebuff.privacy.json` verification receipt. Verification requires the opt-out patch **inside the binary**, so a receipt generated for an official binary is rejected. Docker images build this stage automatically (Linux x64/arm64); see [cli/README.md](cli/README.md).

### 4. Authenticate (When Token Expired)
```bash
./login.sh
```
Follow the browser prompt to complete OAuth login. The helper prefers your verified private CLI; the token is stored in the CLI profile or configured in `.env`.

### 5. Launch Service
```bash
./start.sh
```
Native mode binds `127.0.0.1` by default. The gateway **refuses to start on a non-loopback bind without `FREEBUFF_API_KEY`**:

```bash
export FREEBUFF_API_HOST="0.0.0.0"
export FREEBUFF_API_PORT="8000"
export FREEBUFF_API_KEY="$(python3 -c 'import secrets; print("sk-" + secrets.token_hex(32))')"
./start.sh
```
Clients send that key as `Authorization: Bearer <FREEBUFF_API_KEY>`.

### 6. Or Run the Prebuilt Container

CI publishes a multi-arch image (linux/amd64 + linux/arm64) to GitHub Container Registry on every push to `main` — the image performs the verified private CLI build (step 3) internally:

```bash
docker pull ghcr.io/ryoudyfz/apibuff:latest
docker run -d --name apibuff -p 127.0.0.1:8000:8000 --env-file .env ghcr.io/ryoudyfz/apibuff:latest
```

`.env` must set `FREEBUFF_API_KEY` (the gateway refuses non-loopback binds without it, and containers always bind non-loopback internally). See `docker-compose.yml` for the full environment and volume set. Publishing a GitHub Release (`v*` tag) runs the same pipeline and pushes versioned tags (`1.0.3`, `1.0`) plus `latest` — pre-releases get their version tags but never move `latest` — and appends the image reference to the release notes. Every build also carries its short commit SHA tag.

---

## 📡 API Endpoints

### 1. Health Check
```bash
curl http://127.0.0.1:8000/health
```
Returns local liveness only — no credentials, identity, or upstream probes. **503 while no worker is running**, so containers and start scripts cannot mistake a worker-less gateway for a serving one:
```json
{
  "status": "ok",
  "worker_count": 1,
  "uptime_seconds": 42
}
```

### 2. List Model Catalog
```bash
curl http://127.0.0.1:8000/v1/models
```
Returns the available catalog rows with source/status metadata, pricing and capabilities. The following values illustrate the response shape and are not a current rate quote. Local fallback rows use unknown price and account access:
```json
{
  "object": "list",
  "data": [
    {
      "id": "solar-mini-4",
      "object": "model",
      "created": 1700000000,
      "owned_by": "freebuff",
      "display_name": "Solar Mini 4",
      "tagline": "Fast and light",
      "pricing": {
        "freebucks_per_hour": 0,
        "hours_equivalent": 0.0,
        "is_free": true,
        "notice": "Promotional",
        "off_peak": null
      },
      "capabilities": {
        "context_window": 500000,
        "multimodal": false,
        "efforts": [],
        "default_effort": null,
        "access": "open",
        "plan_required": false,
        "premium": false,
        "data_use": "training",
        "badges": ["Promotional"]
      }
    },
    {
      "id": "glm-5.3-flash",
      "object": "model",
      "display_name": "GLM 5.3 Flash",
      "tagline": "Deep reasoning",
      "pricing": {
        "freebucks_per_hour": 20,
        "hours_equivalent": 4.0,
        "is_free": false,
        "notice": null,
        "off_peak": null
      },
      "capabilities": {
        "context_window": 1000000,
        "efforts": ["low", "high"],
        "default_effort": "high"
      }
    }
  ]
}
```

### 3. Detailed Account Pricing & Quota (`/v1/pricing`)
```bash
curl http://127.0.0.1:8000/v1/pricing
```
Returns the metering status reported for the default credentials, with `source`, `status`, `balance_status` and `metadata`. It is not a combined view of all pooled accounts. If verification fails, `account` and `balance` are `null`, and `error` explains why. Keychain pointers without a readable scoped token are reported as unknown.

When available, the response includes:
- **Freebucks Rule**: 1 hour = 5 Freebucks (standard metering unit on MiMo Flash Slow).
- **Balances**: Total Freebucks and equivalent hours spendable right now.
- **Daily Allowance**: Daily quota, amount spent, remaining units, and Pacific/local reset time.
- **Wallet**: Permanent Freebucks balance.
- **Subscription Tiers**: Available upgrade plans (`Starter`, `Plus`, `Pro`) with session limits and discounts.
- **Rate Cards**: Pricing for every individual model in the fleet.

Sample response:
```json
{
  "account": {
    "access_tier": "limited",
    "country_code": "US",
    "country_verified": false
  },
  "balance": {
    "total_freebucks": 50,
    "total_hours": 10.0,
    "daily": {
      "limit_freebucks": 25,
      "limit_hours": 5.0,
      "spent_freebucks": 5,
      "spent_hours": 1.0,
      "remaining_freebucks": 20,
      "remaining_hours": 4.0,
      "reset_at": "2026-10-24T16:00:00.000Z",
      "reset_timezone": "Asia/Shanghai"
    },
    "wallet": {
      "balance_freebucks": 30,
      "balance_hours": 6.0,
      "monthly_bonus": 0
    }
  },
  "models_count": 57
}
```

### 4. Single Model Inspection
```bash
curl http://127.0.0.1:8000/v1/models/deepseek-v4.1-flash
```

### 5. Chat Completions (Non-Streaming)
```bash
curl -X POST http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "glm-5.3-flash",
    "messages": [
      {"role": "user", "content": "Write a python function to check if a number is prime."}
    ],
    "stream": false
  }'
```

### 6. Chat Completions (Streaming / SSE)
```bash
curl -N -X POST http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "deepseek-v4.1-flash",
    "messages": [
      {"role": "user", "content": "Explain quantum entanglement in 2 sentences."}
    ],
    "stream": true
  }'
```

### 7. Execution Environment Audit & Keystroke Jitter
Inspect the observed execution environment, process ancestry classification, and bounded local typing simulation. Diagnostics accept at most 4096 characters (128 in human typing mode), require finite valid delay/CPS values, and stop after five seconds:

```bash
# 1. Environment & Process Chain Audit (x-freebuff-env)
curl http://127.0.0.1:8000/v1/system/env-audit

# 2. Keystroke Jitter Simulation & CPS Test
curl -X POST http://127.0.0.1:8000/v1/system/jitter-test \
  -H "Content-Type: application/json" \
  -d '{
    "text": "Write a python function to check if a number is prime.",
    "mode": "human"
  }'

# 3. Read Last Recorded Live Keystroke Metrics
curl http://127.0.0.1:8000/v1/system/jitter-metrics
```

### 8. Dynamic Client Workspace Mapping & Real Git Binding
Workspace selection is restricted to the resolved `FREEBUFF_WORKER_WORKDIR` and any operator-configured `FREEBUFF_ALLOWED_WORKSPACE_ROOTS` (colon-separated on Unix). Symlinks resolving outside these roots are rejected. By default, each account uses its own subdirectory under the worker root. To use an external repository, add its root to the allowlist before starting the gateway:

```bash
# 1. Inspect current active workspace & Git metadata
curl http://127.0.0.1:8000/v1/workspace

# 2. Dynamically switch active workspace
# Requires this directory in FREEBUFF_ALLOWED_WORKSPACE_ROOTS.
curl -X POST http://127.0.0.1:8000/v1/workspace \
  -H "Content-Type: application/json" \
  -d '{"path": "/Users/dannyfeng/Documents/MyProject"}'

# 3. Pass workspace via HTTP header (supported by Cline, Continue, Roo Code, etc.)
curl -X POST http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "X-Workspace-Path: /Users/dannyfeng/Documents/MyProject" \
  -d '{
    "model": "glm-5.3-flash",
    "messages": [{"role": "user", "content": "Review recent git commits."}]
  }'
```

*Note*: The gateway automatically discovers and binds workspaces from:
1. `X-Workspace-Path` / `X-Cwd` / `X-Project-Dir` HTTP headers.
2. JSON body `workspace` or `cwd` properties.
3. System/developer prompt strings (e.g. `Current Working Directory: /path/to/project` injected by Cline/Cursor).

Every selection uses the same allowlist. Invalid explicit selections return an error. This restricts directory selection; it is not an OS sandbox for files a CLI process can access. Workspace administration and account/proxy reloads wait for active worker leases to finish. Repository agents are not automatically trusted; `FREEBUFF_TRUST_AGENTS=true` explicitly opts in for an operator-approved repository.

---

## ⚡ Error Interception & Session Isolation

### 1. Sub-Second Fast-Fail for Fatal TUI Error Modals
The gateway monitors the raw PTY stream and `log.jsonl` in real time. Rather than hanging for 120-second timeout intervals when the CLI encounters interactive dialogs or fatal blockers, the gateway immediately unblocks its ready waiter and raises typed HTTP errors:

| Observed Fatal TUI Modal | Upstream Cause | Gateway Error Mapping | HTTP Status |
|---|---|---|:---:|
| `This account is suspended` | Account banned or locked | `FreebuffAuthError(account_suspended)` | **401** |
| `Disable VPN, proxy or Tor traffic` / `detected anonymous network traffic` | Datacenter or anonymized IP detected | `FreebuffPermissionError(anonymous_network_blocked)` | **403** |
| `could not verify your location` / `Freebuff is unavailable in your region` | Unsupported geolocation or broken egress | `FreebuffPermissionError(country_blocked)` | **403** |
| `Not enough hours` / `Session limit reached` | Plan hours or credits exhausted | `FreebuffPaymentError(insufficient_quota)` | **402** |
| `Too many Freebuff sessions on this network` | IP concurrent session cap reached | `FreebuffRateLimitError(ip_capped)` with 30s cooldown | **429** |
| `Press ENTER to login` | Missing or expired token | `FreebuffAuthError(invalid_api_key)` | **401** |

Example JSON error response:
```json
{
  "error": {
    "message": "Freebuff hours / credits exhausted for this account.",
    "type": "insufficient_quota",
    "code": 402
  }
}
```

Errors occurring before the first SSE event retain their corresponding HTTP status codes. Errors occurring after streaming has begun are emitted as an SSE error event followed by `[DONE]`, without a success `finish_reason`.

### 2. Model Admission & Spend Auto-Confirmation
When launching certain promotional or paid models, Freebuff CLI may present interactive TUI dialogs asking for confirmation or retry:
- **Admission Confirmation (`Enter: confirm and send` / `enter: confirm`)**: The worker PTY detects the modal, introduces a human-like visual and cognitive delay (450ms–850ms), and writes `\r` (Enter) to confirm and start generation.
- **Bounded Retry (`Enter: retry`)**: Automatically retries up to 2 times with cognitive delays. If the retry prompt persists beyond 2 attempts, auto-retry halts to prevent endless loops.
- **Operator Toggle**: Enabled by default via `FREEBUFF_ADMISSION_AUTO_CONFIRM=true`. If set to `false`, the worker raises `FreebuffInteractionRequiredError`, requiring manual operator intervention.

### 3. Multi-turn Requests & Ultra-Long Prompt Transport
Send complete system/developer instructions and conversation history on every request, ending with a nonempty `user` message. Every request initiates a fresh CLI chat directory and replays the serialized history. `session_id`, `X-Session-ID`, and `X-Conversation-ID` provide account affinity only; `reset_context=false` does not retain shared CLI state.

- **Ultra-Long Prompts (>2000 UTF-16 units)**: The official CLI switches pastes longer than 2000 characters into file-like text attachments. APIbuff transparently detects pastes over 2000 characters and structures them as native `textAttachments`. This removes the 2000-character barrier, enabling prompts ranging from 32k to 128k+ characters without truncation.
- **Sidecar Optimized Transcript Polling**: Polling checks the `chat-meta.json` signature first, parsing `chat-messages.json` only when the sidecar signature changes, backed by a 1-second safety fallback. This eliminates redundant JSON parsing on unchanged frames during long generations.
- **Input Pacing**: The adapter sends `/new` separately, verifies the reset through directory creation or `/new` input history, and pastes the serialized request with a single `[Current Request]` marker so command-like syntax and paths remain ordinary prompt text. Paste review pauses scale with the pasted length, and each account draws a deterministic terminal geometry from common real-world sizes.
- **Strict Validation**: Text messages and `stream` are fully supported. Unsupported features (tool calls, multimodal inputs, assistant prefill, unsupported sampling flags) return HTTP 422. Token usage is `null` because the gateway has no verified token counts.

---

## 🛡️ Security, Privacy & Anti-Fingerprinting Guardrails

The Freebuff CLI integrates aggressive client-side telemetry, hardware probes, and parent process inspections. APIbuff provides comprehensive privacy safeguards across both macOS and Linux:

### 1. PostHog Telemetry Opt-Out & Private CLI Build Verification
- **Upstream Issue**: Official CLI builds hardcode PostHog project keys and API endpoints directly into bundle literals. Setting environment variables like `NEXT_PUBLIC_POSTHOG_API_KEY=""` has zero effect in the compiled executable. Setting `CODEBUFF_SHIP_LOGS=false` only disables `/api/logs` and leaves PostHog and Axiom tracking active.
- **Private Build Fix**: `scripts/build_private_cli.py` rebuilds the pinned official 0.2.34 release (archive SHA-256, per-target JavaScript digest, and every embedded asset digest pinned via `cli/freebuff-0.2.34-binary-checksums.json` and `cli/assets-0.2.34-*.json`) using Bun with `--env=disable`. Docker images perform this build automatically for Linux x64/arm64.
- **Runtime Interception**: `cli/posthog-opt-out.js` injects an immutable no-op PostHog client at process initialization when `CODEBUFF_DISABLE_POSTHOG=1` or `CODEBUFF_SHIP_LOGS=false` is present. It neutralizes all `capture`, `identify`, `alias`, `captureException`, and `flush` hooks before SDK initialization.
- **Startup Enforcement**: Workers and the container entrypoint verify via `app/cli_build.py` — before touching account credentials — that the binary **embeds the patched PostHog factory** and carries a matching receipt. A receipt generated for an official binary is rejected; there is no fallback to the npm launcher or official executable. See [PostHog Opt-Out Audit](reports/posthog-opt-out-audit-2026-10-25.md) and [cli/README.md](cli/README.md).

### 2. Cross-Platform Hardware Fingerprint Privacy Virtualization (macOS & Linux)
- **Realistic Synthetic Identifiers**: Every value is deterministic per account ID (`app/hardware_shims.py`) and shaped like genuine hardware: a random-form (v4) UUID — Apple's `IOPlatformUUID` values are random v4 form, never namespace-derived v5 — a 12-character serial from Apple's serial alphabet (which omits `I` and `O`), a genuine model identifier (`Mac15,6`, `MacBookPro18,3`, …), and a per-account `<owner>s-<machine>.local` hostname in the macOS default style. Identical synthetic shapes across all accounts would themselves be a fingerprint.
- **macOS Branch**: `app/hardware_preload.cjs` intercepts `os.hostname()` and child process execution paths. A macOS sandbox (`sandbox-exec`) denies raw execution of system `ioreg`, `system_profiler`, and `hostname` binaries, preventing child shells from bypassing shims.
- **Linux Branch**: Virtualizes Linux machine characteristics (`/etc/machine-id`, `/var/lib/dbus/machine-id`, `/sys/class/dmi/id/product_uuid`, `product_serial`, `dmidecode`) via `fake_bin` shims, Node `fs` interception, and optional C-level `LD_PRELOAD` hooks (`libhardware_privacy.so`), skipping macOS-only sandbox requirements.
- **Pre-flight Health Check**: Before launching a worker, the CLI executable runs a synchronous preflight check in an isolated temporary profile with networking denied.
- **Configuration**: Controlled by `FREEBUFF_HARDWARE_PRIVACY=auto` (auto-detects macOS or Linux protection), `macos` (enforced), `linux` (enforced), or `off` (disabled for offline test fixtures). See [Hardware Privacy Audit](reports/hardware-privacy-2026-10-25.md).

### 3. Dual-Platform Process Ancestry Normalization (`p=shell;g=terminal`)
The extracted Freebuff CLI inspects its two parent ancestors to verify whether it runs inside an interactive terminal emulator:
- **macOS**: `app/fake_bin/ps` intercepts queries matching `ps -o ppid=,comm= -p <supervisor_pid>` and reports `/System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal` while preserving PPID. All other `ps` queries pass through to `/bin/ps`.
- **Linux**: The supervisor process sets its own kernel process name to `terminal` using `prctl(PR_SET_NAME, "terminal")` before launching the shell. Both `/proc/<pid>/stat` and system `ps` report `terminal` without requiring root privileges, extra capabilities, or LD_PRELOAD.
- **Configuration**: Controlled via `FREEBUFF_SUPERVISOR_PS_SHIM=true` (default: `true`). Diagnostic endpoint `/v1/system/env-audit` honestly reports real ancestry using absolute system tools. See [Process Ancestry Audit](reports/process-ancestry-2026-10-25.md) and [Linux Process Ancestry Audit](reports/linux-process-ancestry-2026-10-25.md).

### 4. Git Origin Remote Privacy Protection
Worker startup installs `fake_bin/git` and prepends it to `PATH`. When Freebuff runs `git [-C <root>] remote get-url origin` (`--all`/`--push`) to query sponsored project details, the shim returns exit code 128 with empty stdout. This prevents repository `owner/repo` metadata from being harvested. All other Git operations (`status`, `commit`, `diff`, `log`) pass through unmodified.

### 5. Proxy Loopback Virtualization (`px=remote`)
Freebuff evaluates whether its configured proxy resides on loopback addresses (`127.0.0.1`, `localhost`, `[::1]`) and reports `px=loopback`. In `app/hardware_preload.cjs`, loopback proxies are virtualized into `http://proxy.internal:<port>`, ensuring client telemetry classifies the proxy bucket as `remote`. Strict 5-second DNS validation confirms resolution against the listen IP.

### 6. PTY File Descriptor Elevation (`RLIMIT_NOFILE`)
High-concurrency multi-account operations open numerous PTY master/slave pairs, UNIX pipes, and network sockets. `app/lifecycle.py` automatically checks and elevates the process soft file descriptor limit (`RLIMIT_NOFILE`) to 2048+ during startup, preventing file descriptor exhaustion errors.

---

## 🔄 Multi-Account Orchestration & Multi-VPN Proxy Pool

APIbuff supports concurrent multi-account operations with isolated per-account VPN/proxy tunnels, dynamic subscription parsing, and circuit breaking:

### 1. Configuration (`accounts.json`)
Copy `accounts.example.json` to `accounts.json`:
```json
{
  "subscription_url": "https://your-airport.com/api/v1/client/subscribe?token=xxxx",
  "load_balance_strategy": "least_busy",
  "accounts": [
    {
      "id": "acc_01_us",
      "name": "US Account 1",
      "token": "sk-codebuff-token-1",
      "node_filter": "US|America|美",
      "weight": 2,
      "enabled": true
    },
    {
      "id": "acc_02_jp",
      "name": "Japan Account 2",
      "token": "sk-codebuff-token-2",
      "node_filter": "JP|Japan|日本|东京",
      "weight": 1,
      "enabled": true
    },
    {
      "id": "acc_03_sg",
      "name": "Singapore Account 3 (Direct Proxy)",
      "token": "sk-codebuff-token-3",
      "proxy": "socks5://127.0.0.1:1080",
      "weight": 1,
      "enabled": false
    }
  ]
}
```

### 2. Supported VPN & Proxy Protocols (Sing-box Core)
- **Shadowsocks / ShadowsocksR**
- **VMess / VLESS**
- **Trojan**
- **Hysteria / Hysteria 2**
- **TUIC / WireGuard**
- **SOCKS5 / HTTP / HTTPS**

### 3. Load Balancing & Rate Limit Cooldown
- **Least-Busy (Default)**: Dispatches requests to the worker with the lowest in-flight active turns (0 concurrency first).
- **Round-Robin**: Sequentially rotates requests across healthy accounts.
- **Quota-Aware**: Orders by recorded account balance when available.
- **Single Attempt per Request**: Upstream rejections fail cleanly without cross-account retries or concatenation.
- **Failure Semantics**: Only credential, balance, and access verdicts (401/402/403) mark an account terminal until an explicit reload. Timeouts, upstream 5xx, client disconnects, and 429s place the account in a short cooldown (the upstream `Retry-After`, or 30s otherwise) — one dropped connection can no longer take a single-account pool down.
- **Exclusive Exit Nodes**: One physical subscription node is assigned to at most one account. A shared exit IP is both a cross-account correlation signal and the direct trigger for the upstream per-network session cap; an account whose filter only matches already-assigned nodes reports `proxy_error` instead.

### 4. Account & Proxy Management APIs
```bash
# 1. View all accounts, assigned VPN nodes, active turns, and balances
curl http://127.0.0.1:8000/v1/accounts

# 2. Hot-reload accounts and proxies without restarting the gateway
curl -X POST http://127.0.0.1:8000/v1/accounts/reload

# 3. View parsed proxy nodes from subscription
curl http://127.0.0.1:8000/v1/proxies/nodes

# 4. Refresh subscription nodes and rebind inbounds
curl -X POST http://127.0.0.1:8000/v1/proxies/refresh
```

---

## 📦 Per-Account Container Isolation & Lifecycle Management

For strict process, credential, and storage isolation, run dedicated instances of `docker-compose.account.yml` per account:

### 1. Isolated Deployment
Create `.env.alpha` and `.env.beta` from `.env.example`. Set a distinct account token, gateway key, and `FREEBUFF_API_PORT` in each (for example, 8001 and 8002):

```bash
# Launch Account Alpha (Port 8001)
docker compose --env-file .env.alpha -f docker-compose.account.yml -p freebuff-alpha up -d --build

# Launch Account Beta (Port 8002)
docker compose --env-file .env.beta -f docker-compose.account.yml -p freebuff-beta up -d
```

### 2. Storage Paths
Account state is persisted under `$FREEBUFF_SANDBOX_DIR/<id>/.config/manicode` (mapped to `/var/lib/freebuff/accounts` inside containers). Workspace files reside in `/var/lib/freebuff/workspaces`.

### 3. Granular Shutdown Script (`stop.sh`)
The gateway provides clean shutdown controls without resorting to blunt `pkill` commands or open port killing:
```bash
# Stop only Docker Compose services for the default project
./stop.sh --docker

# Stop only native gateways registered by this checkout
./stop.sh --native

# Stop both native and container services
./stop.sh

# Stop a specific isolated account container
docker compose --env-file .env.alpha -f docker-compose.account.yml -p freebuff-alpha stop --timeout 15
```

---

## 🛠️ Model Routing

Use an exact ID, key, provider handle or alias returned by `/v1/models`. Unknown model names return HTTP 404. Names from other model generations (e.g. `gpt-4o`) are not remapped to different models. Omitting `model` uses `FREEBUFF_DEFAULT_MODEL`.

Popular available models in the live fleet:
- `glm-5.3-flash` (Fast, deep reasoning)
- `deepseek-v4.1-flash` (High coding performance)
- `solar-mini-4` (Lightweight promotional)
- Plus 57+ dynamically synchronized models from the live Codebuff catalog.

---

## ⚙️ Configuration Reference

All settings can be configured via environment variables or an `.env` file:

| Variable | Default | Description |
|---|---|---|
| `CODEBUFF_API_KEY` | `""` | Upstream Freebuff / Codebuff OAuth token |
| `FREEBUFF_API_KEY` | `""` | Secret bearer key required by gateway API endpoints; **mandatory for non-loopback binds** (startup refuses otherwise) |
| `FREEBUFF_API_HOST` | `127.0.0.1` (`.env` example) / `0.0.0.0` (code default) | Server bind host address |
| `FREEBUFF_API_PORT` | `8000` | Server listen port |
| `FREEBUFF_BIN` | `.freebuff/private-cli/freebuff` | Absolute path to the verified private CLI executable |
| `FREEBUFF_DEFAULT_MODEL` | `glm-5.3-flash` | Default model when unspecified in requests |
| `FREEBUFF_WORKER_WORKDIR` | `/tmp/freebuff-api-worker` | Base sandbox root for worker instances |
| `FREEBUFF_ALLOWED_WORKSPACE_ROOTS` | `""` | Colon-separated allowed filesystem roots for client workspace binding |
| `FREEBUFF_TURN_TIMEOUT` | `120` | Maximum seconds allowed per generation turn (plus a boot allowance per planned cold start) |
| `FREEBUFF_BOOT_TIMEOUT` | `20` | Maximum seconds waiting for worker ready prompt |
| `FREEBUFF_QUEUE_TIMEOUT` | `15` | Maximum seconds waiting for an available worker lease |
| `FREEBUFF_ADMISSION_AUTO_CONFIRM` | `true` | Auto-confirm model admission prompts and bounded retries |
| `FREEBUFF_TAKEOVER_AUTO_CONFIRM` | `true` | Auto-confirm session takeover prompts |
| `FREEBUFF_SUPERVISOR_PS_SHIM` | `true` | Dual-platform process ancestry normalization (`p=shell;g=terminal`) |
| `FREEBUFF_HARDWARE_PRIVACY` | `auto` | Cross-platform hardware virtualization (`auto`, `macos`, `linux`, `off`) |
| `FREEBUFF_PROXY_LISTEN_IP` | `127.0.0.1` | Local proxy listen IP for Sing-box inbounds |
| `FREEBUFF_PROXY_HOST` | `proxy.internal` | Virtual hostname passed to workers for `px=remote` evaluation |
| `FREEBUFF_JITTER_MODE` | `human` | Keystroke simulation timing profile (`human`, `fast`, `instant`) |
| `FREEBUFF_LIVE_CATALOG` | `false` | Opt-in direct catalog/session/`/me` lookups from the gateway host (offline by default; the CLI inside each worker already fetches the live catalog through its own account proxy) |

---

## 🧪 Testing & Verification

The test suite runs 100% offline with mock CLI fixtures and temporary sandbox directories. It does not submit prompts or consume upstream credits. CI runs the same suite on every push (`.github/workflows/ci.yml`):

```bash
# Run all 339 offline regression tests
python3 tests/run_offline.py
```

### Test Coverage Highlights
- **Terminal Color OSC Replies (`test_terminal_colors.py`)**: Covers OSC 10/11 query chunking, BEL/ST terminators, repeated queries, serial write locks, and latency reduction.
- **Model Admission & Fatal Modals (`test_admission_and_fatal_modals.py`)**: Tests `Enter: confirm and send` auto-confirm, bounded `Enter: retry`, and instant fast-fail for suspended, geo-blocked, anonymous network, quota exhausted, and IP capped modals.
- **Proxy Loopback Virtualization & File Limits (`test_proxy_loopback_and_ulimit.py`)**: Tests `RLIMIT_NOFILE` soft limit elevation and JS preload virtualization from loopback to `proxy.internal`.
- **Hardware Privacy (`test_hardware_privacy.py`)**: Tests synthetic UUID/serial determinism, format validation, platform policy enforcement, and sandbox isolation.
- **Private Build & PostHog Opt-Out (`test_cli_build.py`, `test_posthog_opt_out.mjs`)**: Verifies bundle SHA-256 integrity, receipt validation, and no-op client replacement.
- **Process View Ancestry (`test_process_view.py`)**: Tests macOS `ps` shim and Linux `prctl(PR_SET_NAME, "terminal")`.
- **Git Privacy Shield (`test_git_privacy.py`)**: Validates origin URL exit 128 suppression and pass-through for status/diff/commit.
- **Ultra-Long Prompt Attachments (`test_response_attachments.py`)**: Tests automatic translation of prompts >2000 characters into native `textAttachments`.
- **Worker Lifecycle & Concurrency (`test_worker_lifecycle.py`, `test_worker_turns.py`, `test_multi_account_pool.py`)**: Tests turn lease exclusivity, abrupt exit cleanup via lifetime pipes, and least-busy load distribution.
