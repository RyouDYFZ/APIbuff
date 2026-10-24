# Freebuff OpenAI-Compatible API Gateway

A high-performance, compliant, and secure OpenAI-compatible API proxy for **Freebuff CLI** (`npm install -g freebuff`), architected with reference to mature conversion engines like [sub2api](https://github.com/Wei-Shaw/sub2api).

---

## 🌟 Architecture & Safety Compliance

Freebuff uses an internal anti-bot behavioral fingerprinting system (`x-freebuff-env` / `freebuff_client_env` checking parent process hierarchies and TTY descriptors, plus `freebuff_input_profile` measuring typing rhythms and key cadence). Direct HTTP emulation of `/api/v1/chat/completions` triggers `403 free_mode_cli_required` or risks account suspension.

`freebuff-api-service` implements an **authentic PTY-driven worker architecture**:
1. **Isolated Pseudo-Terminal (PTY)**: Drives the official, authentic `freebuff` binary inside virtual master/slave PTY sessions.
2. **Behavioral Compliance**: Stdin/stdout are real TTY terminals, satisfying all parent-process and environment telemetry natively without spoofing.
3. **Structured State Extraction**: Monitors execution logs (`log.jsonl`) and atomic chat manifests (`chat-messages.json`), guaranteeing zero ANSI escape artifacts and 100% clean markdown.
4. **Dynamic Model Catalog & Pricing**: Direct cryptographic Ed25519 device-signed queries to `/api/v1/freebuff/models` and `/api/v1/freebuff/session` expose the complete 57+ model catalog, live pricing rates (Freebucks/hour), peak/off-peak schedules, and remaining allowances.
5. **Seamless Model Switching**: Automatically updates session settings and dynamically routes completion requests across the full model fleet.

```
┌─────────────────────────────────────────────────────────────┐
│ Clients (Cursor, NextChat, Open WebUI, OpenAI SDK, curl)     │
└──────────────────────────────┬──────────────────────────────┘
                               │ HTTP / SSE (/v1/chat/completions, /v1/pricing)
┌──────────────────────────────▼──────────────────────────────┐
│                  Freebuff API Service (FastAPI)              │
│  - OpenAI Adapter (/v1/models, /v1/chat/completions)        │
│  - Dynamic Catalog & Pricing Engine (Ed25519 Signed)        │
│  - Worker Pool & Automatic Model Router                      │
│  - ANSI Cleaner & Stream Token Extractor                    │
└──────────────────────────────┬──────────────────────────────┘
                               │ PTY IPC (master / slave)
┌──────────────────────────────▼──────────────────────────────┐
│                 Freebuff CLI (OpenTUI Runtime)              │
│  - Authentic Ed25519 WebCrypto Request Signing              │
│  - Hardware Keychain Token Integration                      │
│  - Real-time Keystroke Profile Telemetry                    │
└──────────────────────────────┬──────────────────────────────┘
                               │ HTTPS (Codebuff LLM Gateway)
┌──────────────────────────────▼──────────────────────────────┐
│                      Codebuff Backend                       │
│  (57+ models: MiMo 2.6, GLM 5.3, DeepSeek V4.1, GPT-6, etc.) │
└─────────────────────────────────────────────────────────────┘
```

---

## 🚀 Quick Start

### 1. Requirements
- macOS or Linux
- Python 3.10+
- Freebuff CLI installed (`npm install -g freebuff`) and authenticated (`freebuff login`)

### 2. Install Dependencies
```bash
git clone https://github.com/RyouDYFZ/APIbuff.git
cd APIbuff
pip install -r requirements.txt
```

### 3. Authenticate (When Token Expired)
```bash
./login.sh
```
Follow the browser prompt to complete OAuth login. Token will be securely stored in macOS Keychain.

### 4. Launch Service
```bash
./start.sh
```
The server will start on `http://0.0.0.0:8000`.

To run with custom host, port, or API key:
```bash
export FREEBUFF_API_HOST="127.0.0.1"
export FREEBUFF_API_PORT="8000"
export FREEBUFF_API_KEY="sk-my-secret-key"
./start.sh
```

---

## 📡 API Endpoints

### 1. Health Check
```bash
curl http://127.0.0.1:8000/health
```

### 2. List Full Model Catalog (57+ Models)
```bash
curl http://127.0.0.1:8000/v1/models
```
Returns all 57+ models sorted by price, enriched with **pricing**, **context window**, **reasoning effort ladders**, and **capabilities**:
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
Returns the live metering status:
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
Inspect and test the underlying PTY execution environment and keystroke jitter simulation:

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
Bind incoming requests to the real project directory selected in your client (Cursor, Cline, Roo Code, Continue, etc.) so Freebuff operates on genuine Git branches, commit histories, and codebase context:

```bash
# 1. Inspect current active workspace & Git metadata
curl http://127.0.0.1:8000/v1/workspace

# 2. Dynamically switch active workspace
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
3. System prompt strings (e.g. `Current Working Directory: /path/to/project` injected by Cline/Cursor).

---

## ⚡ Fast-Fail Error Interception & Session Continuity

### 1. Fast-Fail Error Interception (Sub-Second Feedback)
If a user's token expires, rate limits are hit, or an IP block occurs, Freebuff CLI does not emit completion events. Previously, callers hung for 120 seconds before timing out.
`freebuff-api-service` now features **Fast-Fail Error Interception**:
- Monitors PTY output for `Press ENTER to login...` and scans `log.jsonl` for fatal HTTP status codes (401, 429, 403, 5xx).
- Fails in **< 0.05 seconds** with standard OpenAI error structures:
```json
{
  "error": {
    "message": "Freebuff authentication required: token is invalid or expired. Run ./login.sh to authenticate.",
    "type": "authentication_error",
    "code": 401
  }
}
```

### 2. Multi-turn Session Continuity
For conversational pair-programming in Cursor or Cline:
- Supply `session_id` in request body or pass `X-Session-ID` / `X-Conversation-ID` headers.
- If the session matches the active conversation, the gateway **preserves native runtime context** and transmits only the newest user prompt turn, dropping multi-turn response latency to **1~2 seconds**.

---

## 🛠️ Model Routing & Universal Aliases

The API service accepts friendly slugs, provider-qualified IDs, and universal OpenAI/Anthropic/DeepSeek aliases:

| Universal Client Alias | Target Freebuff Model | Rate (Freebucks/hr) | Context Window |
|---|---|---|---|
| `default` / `glm-4` / `glm-5` | GLM 5.3 Flash | 0 (Promotional) | 128k |
| `deepseek-chat` / `deepseek-coder` | DeepSeek v4.1 Flash | 5 | 128k |
| `gpt-4o` / `gpt-4` / `o1` / `o3-mini` | GPT-6 Luna | 25 | 256k |
| `gpt-4o-mini` | GPT-6.1 Sol | 35 | 512k |
| `claude-3-5-sonnet` / `claude-3-7-sonnet` | Claude Haiku 5.5 | 10 | 200k |
| `gemini-2.0-flash` / `gemini-pro` | Gemini 3.8 Flash | 5 | 512k |
| `gemini-1.5-pro` / `gemini-2.5` | Gemini 4 Argon | 30 | 1,000,000 (1M) |
| `mimo` / `mimo-fast` | Mimo v2.6 Flash Fast | 0 | 128k |
| `solar` / `solar-mini` | Solar Mini 4 | 0 | 64k |

*(Plus all 57+ dynamically synchronized models from the live Codebuff catalog)*.

---

## ☁️ Standalone Free Cloud Server Deployment (100% Decoupled from Local Mac)

This gateway is 100% self-contained and cross-platform (supporting Linux x86_64, Linux ARM64, and macOS). It can be deployed to any free cloud server or container platform without touching your local machine.

### Method 1: Render.com (Recommended Free Cloud PaaS, Public HTTPS)
[Render](https://render.com) provides a generous free tier for Docker Web Services with automatic HTTPS and global CDN.

1. **Push this project** to a GitHub repository (public or private).
2. Go to [Render Dashboard](https://dashboard.render.com/) -> **New** -> **Web Service**.
3. Connect your GitHub repository:
   - **Environment**: `Docker`
   - **Region**: `Oregon (US West)` or `Frankfurt`
   - **Plan**: `Free`
4. Add **Environment Variables**:
   - `CODEBUFF_API_KEY`: Your upstream token (from your credentials or `./login.sh`).
   - `FREEBUFF_API_KEY`: Your secret API key to protect your gateway (e.g. `sk-my-cloud-secret`).
   - `FREEBUFF_DEFAULT_MODEL`: `glm-5.3-flash`
5. Click **Deploy Web Service**.
6. When complete, Render will give you a public URL (e.g. `https://freebuff-gateway.onrender.com`).
   - **OpenAI Base URL**: `https://freebuff-gateway.onrender.com/v1`
   - **API Key**: `sk-my-cloud-secret`

*(Alternatively, use the included [`render.yaml`](render.yaml) for a 1-click Blueprint deployment).*

---

### Method 2: Hugging Face Spaces (100% Free, 16GB RAM, No Credit Card)
[Hugging Face Spaces](https://huggingface.co/spaces) offers free Docker-based hosting with 16GB RAM and permanent uptime.

1. Create a new Space on Hugging Face:
   - **SDK**: `Docker` -> `Blank`
   - **Space Hardware**: `CPU basic · 2 vCPU · 16 GB · Free`
2. Clone or push this repository to your Space:
   ```bash
   git remote add space https://huggingface.co/spaces/YOUR_USERNAME/YOUR_SPACE_NAME
   git push space main
   ```
3. In your Space **Settings** -> **Variables and secrets**, add:
   - Secret: `CODEBUFF_API_KEY` = `<your_upstream_token>`
   - Secret: `FREEBUFF_API_KEY` = `<your_gateway_secret>`
4. The Space will build and boot automatically, exposing `https://YOUR_USERNAME-YOUR_SPACE_NAME.hf.space/v1`.

---

### Method 3: Oracle Cloud Free Tier / Any Free Linux VPS (Docker Compose)
Oracle Cloud provides **Always Free** ARM64 Ampere instances (4 OCPUs, 24GB RAM) or AMD x86 instances.

1. SSH into your remote server:
   ```bash
   ssh root@your-server-ip
   ```
2. Clone or upload this repository:
   ```bash
   git clone <your-repo-url> /opt/freebuff-api-service
   cd /opt/freebuff-api-service
   ```
3. Copy environment configuration:
   ```bash
   cp .env.example .env
   # Edit .env and enter your CODEBUFF_API_KEY and FREEBUFF_API_KEY
   nano .env
   ```
4. Start with Docker Compose:
   ```bash
   docker compose up -d
   ```
5. Verify health:
   ```bash
   curl http://localhost:8000/health
   ```

---

### Method 4: Bare-Metal Linux VPS (Systemd Native Installation)
If your free VPS doesn't have Docker installed (Ubuntu 20.04+, Debian 11/12):

1. Run the one-command installer:
   ```bash
   sudo bash scripts/setup_linux.sh
   ```
2. Set your token in `/opt/freebuff-api-service/.env`.
3. Start the systemd service:
   ```bash
   sudo systemctl restart freebuff-api.service
   sudo systemctl status freebuff-api.service
   ```

---

## 🧪 Testing

Run the automated test suites:

```bash
# 1. Fast-Fail, Fallback Catalog, Auth Probe & Session Continuity
python3 tests/test_error_and_session.py

# 2. Dynamic Workspace Mapping & Git Binding Verification
python3 tests/test_workspace_mapping.py

# 3. Terminal Environment & Keystroke Jitter Verification
python3 tests/test_refactored_env.py
```
All suites pass 100% out-of-the-box.
