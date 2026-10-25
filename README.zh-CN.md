# APIbuff｜Freebuff OpenAI API 网关

[English](README.md) | **简体中文**

为 **Freebuff CLI**（`npm install -g freebuff`）提供的 OpenAI 兼容文本 API 代理，架构参考了 [sub2api](https://github.com/Wei-Shaw/sub2api) 等转换引擎。

---

## 🌟 架构与安全合规

Freebuff CLI 会采集并上报一些环境与输入特征信息。

`freebuff-api-service` 实现了 **PTY 驱动工作进程架构**：

1. **隔离的伪终端（PTY）**：在虚拟 master/slave PTY 会话中、以真实 TTY 终端描述符驱动原生 `freebuff` 二进制。
2. **即时应答 OSC 10/11 颜色探测查询**：自动拦截并应答 OpenTUI 终端主题查询（`\x1b]11;rgb:0000/0000/0000\x07` / `\x1b]10;rgb:ffff/ffff/ffff\x07`），可跨分片的 PTY 数据块处理，消除终端探测超时，每次工作进程回合节省约 1.0 秒 CLI 启动时间。
3. **模型准入与扣费确认自动处理**：检测交互式 TUI 准入确认弹窗（`Enter: confirm and send`）与有界重试提示（`Enter: retry`），在模拟人类阅读停顿后安全地自动确认。
4. **致命 TUI 弹窗亚秒级快速失败**：持续扫描 PTY 流，拦截致命的账户、网络与配额弹窗（账户封禁、匿名网络拦截、地域封锁、时长耗尽、IP 并发上限），无需等待 120 秒请求超时。
5. **关联的公开响应**：将输出绑定到 `chat-messages.json` 中新增的用户消息，读取公开响应块，并要求存在成功的完成记录。错误与超时绝不会回退到更早的回答或终端屏幕文本。
6. **超长提示词附件传输**：超过 CLI 2000 UTF-16 字符阈值的提示词会自动打包为原生 CLI 文本附件（`textAttachments`），无缝支持 32k–128k 字符上下文窗口而不截断。
7. **工作进程生命周期与进程隔离**：每个 PTY 工作进程都由守护进程监督；当网关闭其生命周期管道时（包括网关异常退出或被 SIGKILL 之后），守护进程会终止其进程组。
8. **安全、隐私与反指纹防护**：内置 macOS 硬件指纹虚拟化、双平台进程谱系归一化（`p=shell;g=terminal`）、PostHog 遥测停用、Git 远程 URL 隐私保护以及代理回环虚拟化。
9. **模型目录与定价**：加密的 Ed25519 设备签名目录查询可展示 57+ 模型、实时定价（Freebucks/小时）、高峰/非高峰时段与剩余额度。凭据缺失时显示标注为未知的值。
10. **独占的工作进程回合**：一次租约覆盖模型/工作区切换与生成。每个请求都以提供的会话历史开启一个新的 CLI 聊天。

```
┌──────────────────────────────────────────────────────────────┐
│ 客户端（Cursor、Cline、Roo Code、NextChat、OpenAI SDK、curl）   │
└──────────────────────────────┬───────────────────────────────┘
                               │ HTTP / SSE（/v1/chat/completions、/v1/pricing）
┌──────────────────────────────▼───────────────────────────────┐
│                         Freebuff API 服务（FastAPI）          │
│  - OpenAI 适配层（/v1/models、/v1/chat/completions）           │
│  - 动态目录与定价引擎（Ed25519 签名）                   	       │
│  - 工作进程池与自动模型路由                         	       │
│  - 实时 PTY OSC 10/11 颜色流应答器                     	       │
│  - 亚秒级致命弹窗与准入拦截器                            	       │
│  - 超长提示词文本附件打包器                             	       │
│  - 文件描述符提升与资源生命周期                         	       │
└──────────────────────────────┬───────────────────────────────┘
                               │ PTY IPC（master / slave）
┌──────────────────────────────▼───────────────────────────────┐
│                     Freebuff CLI 工作进程守护与运行时 	       │
│  - 双平台进程谱系伪装（p=shell;g=terminal）          	       │
│  - macOS 硬件指纹虚拟化（UUID/序列号）           		       │
│  - PostHog 遥测停用与私有 CLI 完整性构建                 	       │
│  - Git 远程仓库隐私屏蔽（fake_bin/git）                         │
│  - 代理回环虚拟化（px=remote）                                  │
│  - 实时击键节奏遥测与抖动                                	       │
└──────────────────────────────┬───────────────────────────────┘
                               │ HTTPS / TLS（Sing-box 出站）
┌──────────────────────────────▼───────────────────────────────┐
│                        Codebuff 后端                         │
│   （57+ 模型：MiMo 2.6、GLM 5.3、DeepSeek V4.1、GPT-6 等）      │
└──────────────────────────────────────────────────────────────┘
```

---

## 🚀 快速开始

### 1. 环境要求
- macOS 或 Linux

- Python 3.11+

- 私有 Freebuff CLI 构建（`scripts/build_private_cli.py`）

  > [!NOTE]
  > 工作进程在官方 CLI 上会安全失败（fail closed），因为官方 CLI 编译进二进制的 PostHog 遥测会忽略所有停用开关。

- `PATH` 中有 Bun（`npm install -g bun@1.3`，用于构建私有 CLI）

### 2. 安装依赖
```bash
git clone https://github.com/RyouDYFZ/APIbuff.git
cd APIbuff
pip install -r requirements.txt
# 或固定到经过验证的精确依赖集：
pip install -r requirements.lock
```

### 3. 构建私有验证 CLI（必需）
下载固定的官方 0.2.34 发布归档，校验其 SHA-256，解压并以内嵌 PostHog 停用补丁重新构建：
```bash
python3 scripts/build_private_cli.py --fetch
```
该命令会生成 `.freebuff/private-cli/freebuff` 及其 `freebuff.privacy.json` 验证回执。验证要求停用补丁**位于二进制内部**，因此为官方二进制生成的回执会被拒绝。Docker 镜像会自动执行该构建阶段（Linux x64/arm64）；参见 [cli/README.md](cli/README.md)。

### 4. 登录认证（令牌过期时）
```bash
./login.sh
```
按浏览器提示完成 OAuth 登录。该脚本优先使用已验证的私有 CLI；令牌存储在 CLI profile 中，或配置到 `.env`。

### 5. 启动服务
```bash
./start.sh
```
原生模式默认绑定 `127.0.0.1`。网关**在没有 `FREEBUFF_API_KEY` 时拒绝非回环绑定启动**：

```bash
export FREEBUFF_API_HOST="0.0.0.0"
export FREEBUFF_API_PORT="8000"
export FREEBUFF_API_KEY="$(python3 -c 'import secrets; print("sk-" + secrets.token_hex(32))')"
./start.sh
```
客户端以 `Authorization: Bearer <FREEBUFF_API_KEY>` 形式发送该密钥。

### 6. 或运行预构建容器

每次推送到 `main` 时，CI 都会向 GitHub Container Registry 发布多架构镜像（linux/amd64 + linux/arm64）。镜像内部会自动完成（第 3 步的）私有 CLI 验证构建：

```bash
docker pull ghcr.io/ryoudyfz/apibuff:latest
docker run -d --name apibuff -p 127.0.0.1:8000:8000 --env-file .env ghcr.io/ryoudyfz/apibuff:latest
```

`.env` 必须设置 `FREEBUFF_API_KEY`（没有它网关拒绝非回环绑定，而容器内部始终是非回环绑定）。完整的环境变量与卷配置见 `docker-compose.yml`。发布 GitHub Release（`v*` 标签）会运行同一流水线并推送带版本号的标签（`1.0.3`、`1.0`）以及 `latest`——预发布会获得其版本标签但绝不会移动 `latest`——并将镜像引用附加到 Release 说明中。每次构建还附带短提交 SHA 标签。

---

## 📡 API 端点

### 1. 健康检查
```bash
curl http://127.0.0.1:8000/health
```
仅返回本地存活状态。**没有工作进程运行时返回 503。**
```json
{
  "status": "ok",
  "worker_count": 1,
  "uptime_seconds": 42
}
```

### 2. 列出模型目录
```bash
curl http://127.0.0.1:8000/v1/models
```
返回可用目录条目及其来源/状态元数据、定价与能力信息。本地回退条目使用未知价格与账户访问级别：
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

### 3. 账户定价与配额详情（`/v1/pricing`）
```bash
curl http://127.0.0.1:8000/v1/pricing
```
返回默认凭据的计量状态，包含 `source`、`status`、`balance_status` 与 `metadata`。它不是所有池化账户的合并视图。若验证失败，`account` 和 `balance` 为 `null`，`error` 会说明原因。没有可读作用域令牌的钥匙串指针会被报告为未知。

可用时，响应包含：
- **Freebucks 规则**：1 小时 = 5 Freebucks（MiMo Flash Slow 上的标准计量单位）。
- **余额**：当前可消耗的总 Freebucks 与等效小时数。
- **每日额度**：每日配额、已消耗量、剩余额度，以及太平洋时间/本地重置时间。
- **钱包**：永久 Freebucks 余额。
- **订阅档位**：可用的升级计划（`Starter`、`Plus`、`Pro`）及其会话限制与折扣。
- **费率卡**：机群中每个模型的单独定价。

响应示例：
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

### 4. 单模型查询
```bash
curl http://127.0.0.1:8000/v1/models/deepseek-v4.1-flash
```

### 5. 对话补全（非流式）
```bash
curl -X POST http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "glm-5.3-flash",
    "messages": [
      {"role": "user", "content": "写一个判断素数的 Python 函数。"}
    ],
    "stream": false
  }'
```

### 6. 对话补全（流式 / SSE）
```bash
curl -N -X POST http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "deepseek-v4.1-flash",
    "messages": [
      {"role": "user", "content": "用两句话解释量子纠缠。"}
    ],
    "stream": true
  }'
```

### 7. 执行环境审计与击键抖动
查看观测到的执行环境、进程谱系分类以及有界的本地打字模拟。诊断最多接受 4096 字符（human 打字模式下为 128），要求有限且合法的 delay/CPS 数值，并在五秒后停止：

```bash
# 1. 环境与进程链审计（x-freebuff-env）
curl http://127.0.0.1:8000/v1/system/env-audit

# 2. 击键抖动模拟与 CPS 测试
curl -X POST http://127.0.0.1:8000/v1/system/jitter-test \
  -H "Content-Type: application/json" \
  -d '{
    "text": "Write a python function to check if a number is prime.",
    "mode": "human"
  }'

# 3. 读取最近一次记录的实时击键指标
curl http://127.0.0.1:8000/v1/system/jitter-metrics
```

### 8. 动态客户端工作区映射与真实 Git 绑定
工作区选择被限制在解析后的 `FREEBUFF_WORKER_WORKDIR` 以及操作者配置的 `FREEBUFF_ALLOWED_WORKSPACE_ROOTS`（Unix 上以冒号分隔）之内。解析到这些根目录之外的符号链接会被拒绝。默认情况下，每个账户使用工作进程根目录下自己的子目录。要使用外部仓库，请在启动网关前将其根目录加入允许列表：

```bash
# 1. 查看当前活跃工作区与 Git 元数据
curl http://127.0.0.1:8000/v1/workspace

# 2. 动态切换活跃工作区
# 要求该目录位于 FREEBUFF_ALLOWED_WORKSPACE_ROOTS 中。
curl -X POST http://127.0.0.1:8000/v1/workspace \
  -H "Content-Type: application/json" \
  -d '{"path": "/Users/dannyfeng/Documents/MyProject"}'

# 3. 通过 HTTP 头传递工作区（Cline、Continue、Roo Code 等支持）
curl -X POST http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "X-Workspace-Path: /Users/dannyfeng/Documents/MyProject" \
  -d '{
    "model": "glm-5.3-flash",
    "messages": [{"role": "user", "content": "Review recent git commits."}]
  }'
```

*说明*：网关会自动从以下位置发现并绑定工作区：
1. `X-Workspace-Path` / `X-Cwd` / `X-Project-Dir` HTTP 头。
2. JSON 请求体中的 `workspace` 或 `cwd` 属性。
3. 系统/开发者提示词字符串（例如 Cline/Cursor 注入的 `Current Working Directory: /path/to/project`）。

所有选择都使用同一允许列表。无效的显式选择会返回错误。这仅限制目录选择，并不是对 CLI 进程可访问文件的操作系统级沙箱。工作区管理与账户/代理重载会等待活跃的工作进程租约结束。仓库代理不会被自动信任；`FREEBUFF_TRUST_AGENTS=true` 为操作者批准的仓库显式开启。

---

## ⚡ 错误拦截与会话隔离

### 1. 致命 TUI 错误弹窗亚秒级快速失败
网关实时监控原始 PTY 流和 `log.jsonl`。当 CLI 遇到交互式对话框或致命阻塞时，网关不会僵持等待 120 秒超时，而是立即解除就绪等待并抛出带类型的 HTTP 错误：

| 观察到的致命 TUI 弹窗 | 上游原因 | 网关错误映射 | HTTP 状态码 |
|---|---|---|:---:|
| `This account is suspended` | 账户被封禁或锁定 | `FreebuffAuthError(account_suspended)` | **401** |
| `Disable VPN, proxy or Tor traffic` / `detected anonymous network traffic` | 检测到数据中心或匿名化 IP | `FreebuffPermissionError(anonymous_network_blocked)` | **403** |
| `could not verify your location` / `Freebuff is unavailable in your region` | 地理位置不受支持或出口网络异常 | `FreebuffPermissionError(country_blocked)` | **403** |
| `Not enough hours` / `Session limit reached` | 套餐时长或额度耗尽 | `FreebuffPaymentError(insufficient_quota)` | **402** |
| `Too many Freebuff sessions on this network` | 达到 IP 并发会话上限 | `FreebuffRateLimitError(ip_capped)`（30 秒冷却） | **429** |
| `Press ENTER to login` | 令牌缺失或过期 | `FreebuffAuthError(invalid_api_key)` | **401** |

错误响应 JSON 示例：
```json
{
  "error": {
    "message": "Freebuff hours / credits exhausted for this account.",
    "type": "insufficient_quota",
    "code": 402
  }
}
```

在首个 SSE 事件之前发生的错误保留相应的 HTTP 状态码。流式开始后发生的错误以 SSE 错误事件发出并紧跟 `[DONE]`，且不带有表示成功的 `finish_reason`。

### 2. 模型准入与扣费自动确认
启动某些促销或付费模型时，Freebuff CLI 可能弹出要求确认或重试的交互式 TUI 对话框：
- **准入确认（`Enter: confirm and send` / `enter: confirm`）**：工作进程 PTY 检测到该弹窗后，引入类人的视觉与认知停顿（450ms–850ms），随后写入 `\r`（回车）确认并开始生成。
- **有界重试（`Enter: retry`）**：带认知停顿地自动重试，最多 2 次。若重试提示在 2 次尝试后仍然存在，自动重试会停止以防死循环。
- **操作者开关**：默认通过 `FREEBUFF_ADMISSION_AUTO_CONFIRM=true` 启用。若设为 `false`，工作进程会抛出 `FreebuffInteractionRequiredError`，要求操作者手动介入。

### 3. 多轮请求与超长提示词传输
每次请求都发送完整的系统/开发者指令和会话历史，并以非空的 `user` 消息结尾。每个请求都会开启全新的 CLI 聊天目录并重放序列化后的历史。`session_id`、`X-Session-ID` 与 `X-Conversation-ID` 仅提供账户亲和性；`reset_context=false` 不会保留共享的 CLI 状态。

- **超长提示词（>2000 UTF-16 单元）**：官方 CLI 会把超过 2000 字符的粘贴切换为类文件文本附件。APIbuff 透明检测超过 2000 字符的粘贴，并将其构造为原生 `textAttachments`。这消除了 2000 字符壁垒，使 32k 到 128k+ 字符的提示词不被截断。
- **旁路文件优化的转录轮询**：轮询先检查 `chat-meta.json` 签名，仅当旁路签名变化时才解析 `chat-messages.json`，并以 1 秒安全回退兜底。这消除了长生成过程中对未变化帧的冗余 JSON 解析。
- **输入节奏**：适配器单独发送 `/new`，通过目录创建或 `/new` 输入历史验证重置，然后以单个 `[Current Request]` 标记粘贴序列化请求，使类命令语法与路径保持为普通提示词文本。粘贴审查停顿随粘贴长度缩放，每个账户从常见真实终端尺寸中抽取确定性的终端几何参数。
- **严格校验**：完全支持文本消息与 `stream`。不支持的功能（工具调用、多模态输入、助手预填充、不支持的采样参数）返回 HTTP 422。Token 用量为 `null`，因为网关没有经核实的 token 计数。

---

## 🛡️ 安全、隐私与反指纹防护

Freebuff CLI 内置了激进的客户端遥测、硬件探测与父进程检查。APIbuff 在 macOS 与 Linux 上都提供了全面的隐私防护：

### 1. PostHog 遥测停用与私有 CLI 构建验证
- **上游**：官方 CLI 构建把 PostHog 项目密钥与 API 端点直接硬编码进 bundle 字面量。设置 `NEXT_PUBLIC_POSTHOG_API_KEY=""` 之类的环境变量对编译后的可执行文件毫无作用。设置 `CODEBUFF_SHIP_LOGS=false` 只会禁用 `/api/logs`，PostHog 与 Axiom 追踪依然活跃。
- **私有构建修复**：`scripts/build_private_cli.py` 使用 Bun `--env=disable` 重新构建固定的官方 0.2.34 发布（归档 SHA-256、每个目标的 JavaScript 摘要以及每个内嵌资产摘要均通过 `cli/freebuff-0.2.34-binary-checksums.json` 与 `cli/assets-0.2.34-*.json` 固定）。Docker 镜像会为 Linux x64/arm64 自动完成该构建。
- **运行时拦截**：当存在 `CODEBUFF_DISABLE_POSTHOG=1` 或 `CODEBUFF_SHIP_LOGS=false` 时，`cli/posthog-opt-out.js` 会在进程初始化时注入不可变的空操作（no-op）PostHog 客户端，在 SDK 初始化前中和所有 `capture`、`identify`、`alias`、`captureException` 与 `flush` 钩子。
- **启动强制校验**：工作进程与容器入口点会在接触账户凭据之前，通过 `app/cli_build.py` 验证二进制**内嵌了打过补丁的 PostHog 工厂**并携带匹配的回执。为官方二进制生成的回执会被拒绝；不存在回退到 npm 启动器或官方可执行文件的路径。参见 [cli/README.md](cli/README.md)。

### 2. 跨平台硬件指纹隐私虚拟化（macOS 与 Linux）
- **逼真的合成标识符**：每个值都按账户 ID 确定性生成（`app/hardware_shims.py`），且形状与真实硬件一致：随机形式（v4）UUID——Apple 的 `IOPlatformUUID` 值是随机 v4 形式，绝非命名空间派生的 v5——来自 Apple 序列号字母表（省略 `I` 和 `O`）的 12 位序列号、真实的机型标识符（`Mac15,6`、`MacBookPro18,3` 等），以及 macOS 默认风格的 `<owner>s-<machine>.local` 主机名。所有账户共享同样的合成形状本身就是一种指纹。
- **macOS 分支**：`app/hardware_preload.cjs` 拦截 `os.hostname()` 与子进程执行路径。macOS 沙箱（`sandbox-exec`）拒绝直接执行系统的 `ioreg`、`system_profiler` 与 `hostname` 二进制，防止子 shell 绕过 shim。
- **Linux 分支**：通过 `fake_bin` shim、Node `fs` 拦截以及可选的 C 级 `LD_PRELOAD` 钩子（`libhardware_privacy.so`）虚拟化 Linux 机器特征（`/etc/machine-id`、`/var/lib/dbus/machine-id`、`/sys/class/dmi/id/product_uuid`、`product_serial`、`dmidecode`），跳过 macOS 专属的沙箱要求。
- **预检健康检查**：启动工作进程前，CLI 可执行文件会在禁用网络的隔离临时 profile 中运行一次同步预检。
- **配置**：由 `FREEBUFF_HARDWARE_PRIVACY=auto`（自动检测 macOS 或 Linux 保护）、`macos`（强制）、`linux`（强制）或 `off`（为离线测试夹具禁用）控制。参见 [硬件隐私审计](reports/hardware-privacy-2026-10-25.md)。

### 3. 双平台进程谱系归一化（`p=shell;g=terminal`）
逆向提取的 Freebuff CLI 会检查其上两级父进程，以验证自己是否运行在交互式终端模拟器内：
- **macOS**：`app/fake_bin/ps` 拦截匹配 `ps -o ppid=,comm= -p <supervisor_pid>` 的查询并报告 `/System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal`，同时保留 PPID。其他所有 `ps` 查询原样透传给 `/bin/ps`。
- **Linux**：守护进程在启动 shell 之前用 `prctl(PR_SET_NAME, "terminal")` 把自己的内核进程名设置为 `terminal`。`/proc/<pid>/stat` 与系统 `ps` 都会报告 `terminal`，无需 root、额外 capability 或 LD_PRELOAD。
- **配置**：通过 `FREEBUFF_SUPERVISOR_PS_SHIM=true`（默认 `true`）控制。诊断端点 `/v1/system/env-audit` 会使用绝对系统工具诚实报告真实谱系。参见 [进程谱系审计](reports/process-ancestry-2026-10-25.md) 与 [Linux 进程谱系审计](reports/linux-process-ancestry-2026-10-25.md)。

### 4. Git 远程仓库隐私保护
工作进程启动时会安装 `fake_bin/git` 并将其置于 `PATH` 最前。当 Freebuff 运行 `git [-C <root>] remote get-url origin`（`--all`/`--push`）查询赞助项目详情时，该 shim 返回退出码 128 与空 stdout，防止仓库 `owner/repo` 元数据被收集。其他所有 Git 操作（`status`、`commit`、`diff`、`log`）原样透传。

### 5. 代理回环虚拟化（`px=remote`）
Freebuff 会判断其配置的代理是否位于回环地址（`127.0.0.1`、`localhost`、`[::1]`）并上报 `px=loopback`。在 `app/hardware_preload.cjs` 中，回环代理被虚拟化为 `http://proxy.internal:<port>`，确保客户端遥测把代理桶归类为 `remote`。严格的 5 秒 DNS 校验会确认解析与监听 IP 一致。

### 6. PTY 文件描述符提升（`RLIMIT_NOFILE`）
高并发多账户操作会打开大量 PTY master/slave 对、UNIX 管道与网络套接字。`app/lifecycle.py` 会在启动期间自动检查并把进程软文件描述符上限（`RLIMIT_NOFILE`）提升到 2048+，避免文件描述符耗尽错误。

---

## 🔄 多账户编排与多 VPN 代理池

APIbuff 支持多账户并发操作，提供按账户隔离的 VPN/代理隧道、动态订阅解析与熔断：

### 1. 配置（`accounts.json`）
将 `accounts.example.json` 复制为 `accounts.json`：
```json
{
  "subscription_url": "https://your-airport.com/api/v1/client/subscribe?token=xxxx",
  "load_balance_strategy": "least_busy",
  "accounts": [
    {
      "id": "acc_01_us",
      "name": "美国 1",
      "token": "sk-codebuff-token-1",
      "node_filter": "US|America|美国",
      "weight": 2,
      "enabled": true
    },
    {
      "id": "acc_02_jp",
      "name": "日本 2",
      "token": "sk-codebuff-token-2",
      "node_filter": "JP|Japan|日本",
      "weight": 1,
      "enabled": true
    },
    {
      "id": "acc_03_sg",
      "name": "新加坡 3（直连代理）",
      "token": "sk-codebuff-token-3",
      "proxy": "socks5://127.0.0.1:1080",
      "weight": 1,
      "enabled": false
    }
  ]
}
```

### 2. 支持的 VPN 与代理协议（Sing-box 核心）
- **Shadowsocks / ShadowsocksR**
- **VMess / VLESS**
- **Trojan**
- **Hysteria / Hysteria 2**
- **TUIC / WireGuard**
- **SOCKS5 / HTTP / HTTPS**

### 3. 负载均衡与限流冷却
- **最闲优先（默认）**：把请求分发给正在进行的活跃回合最少（并发为 0 优先）的工作进程。
- **轮询（Round-Robin）**：在健康账户间顺序轮转请求。
- **配额感知（Quota-Aware）**：在可用时按记录的账户余额排序。
- **每请求单次尝试**：上游拒绝会干净地失败，不会跨账户重试或拼接。
- **失败语义**：只有凭据、余额与访问判定（401/402/403）会把账户标记为终态，直到显式重载。超时、上游 5xx、客户端断开与 429 只让账户进入短暂冷却（上游 `Retry-After`，否则 30 秒）——一次断连再也打不垮单账户池。
- **独占出口节点**：一个物理订阅节点至多分配给一个账户。共享出口 IP 既是跨账户关联信号，也是上游按网络会话上限的直接触发器；过滤器只匹配到已分配节点的账户会报告 `proxy_error`。

### 4. 账户与代理管理 API
```bash
# 1. 查看所有账户、分配的 VPN 节点、活跃回合与余额
curl http://127.0.0.1:8000/v1/accounts

# 2. 不重启网关热重载账户与代理
curl -X POST http://127.0.0.1:8000/v1/accounts/reload

# 3. 查看从订阅解析出的代理节点
curl http://127.0.0.1:8000/v1/proxies/nodes

# 4. 刷新订阅节点并重新绑定入站
curl -X POST http://127.0.0.1:8000/v1/proxies/refresh
```

---

## 📦 按账户的容器隔离与生命周期管理

如需严格的进程、凭据与存储隔离，可为每个账户运行独立的 `docker-compose.account.yml` 实例：

### 1. 隔离部署
从 `.env.example` 创建 `.env.alpha` 与 `.env.beta`。为每个文件设置不同的账户令牌、网关密钥与 `FREEBUFF_API_PORT`（例如 8001 与 8002）：

```bash
# 启动账户 Alpha（端口 8001）
docker compose --env-file .env.alpha -f docker-compose.account.yml -p freebuff-alpha up -d --build

# 启动账户 Beta（端口 8002）
docker compose --env-file .env.beta -f docker-compose.account.yml -p freebuff-beta up -d
```

### 2. 存储路径
账户状态持久化在 `$FREEBUFF_SANDBOX_DIR/<id>/.config/manicode`（容器内映射到 `/var/lib/freebuff/accounts`）。工作区文件位于 `/var/lib/freebuff/workspaces`。

### 3. 细粒度停机脚本（`stop.sh`）
网关提供干净的停机控制，无需粗暴的 `pkill` 或按端口杀进程：
```bash
# 仅停止默认项目的 Docker Compose 服务
./stop.sh --docker

# 仅停止本检出注册的原生网关
./stop.sh --native

# 同时停止原生与容器服务
./stop.sh

# 停止某个隔离的账户容器
docker compose --env-file .env.alpha -f docker-compose.account.yml -p freebuff-alpha stop --timeout 15
```

---

## 🛠️ 模型路由

使用 `/v1/models` 返回的精确 ID、key、provider 句柄或别名。未知模型名返回 HTTP 404。其他模型世代的名称（如 `gpt-4o`）不会被重映射到别的模型。省略 `model` 时使用 `FREEBUFF_DEFAULT_MODEL`。

实时机群中的热门可用模型：
- `glm-5.3-flash`（快速、深度推理）
- `deepseek-v4.1-flash`（高编码性能）
- `solar-mini-4`（轻量促销）
- 以及来自实时 Codebuff 目录的 57+ 动态同步模型。

---

## ⚙️ 配置参考

所有设置均可通过环境变量或 `.env` 文件配置：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `CODEBUFF_API_KEY` | `""` | 上游 Freebuff / Codebuff OAuth 令牌 |
| `FREEBUFF_API_KEY` | `""` | 网关 API 端点所需的 Bearer 密钥；**非回环绑定时强制要求**（否则拒绝启动） |
| `FREEBUFF_API_HOST` | `127.0.0.1`（`.env` 示例）/ `0.0.0.0`（代码默认） | 服务绑定主机地址 |
| `FREEBUFF_API_PORT` | `8000` | 服务监听端口 |
| `FREEBUFF_BIN` | `.freebuff/private-cli/freebuff` | 已验证私有 CLI 可执行文件的绝对路径 |
| `FREEBUFF_DEFAULT_MODEL` | `glm-5.3-flash` | 请求未指定模型时的默认模型 |
| `FREEBUFF_WORKER_WORKDIR` | `/tmp/freebuff-api-worker` | 工作进程实例的基础沙箱根目录 |
| `FREEBUFF_ALLOWED_WORKSPACE_ROOTS` | `""` | 冒号分隔的客户端工作区绑定允许的文件系统根目录 |
| `FREEBUFF_TURN_TIMEOUT` | `120` | 每个生成回合允许的最大秒数（另加每次计划内冷启动的启动余量） |
| `FREEBUFF_BOOT_TIMEOUT` | `20` | 等待工作进程就绪提示的最大秒数 |
| `FREEBUFF_QUEUE_TIMEOUT` | `15` | 等待可用工作进程租约的最大秒数 |
| `FREEBUFF_ADMISSION_AUTO_CONFIRM` | `true` | 自动确认模型准入提示与有界重试 |
| `FREEBUFF_TAKEOVER_AUTO_CONFIRM` | `true` | 自动确认会话接管提示 |
| `FREEBUFF_SUPERVISOR_PS_SHIM` | `true` | 双平台进程谱系归一化（`p=shell;g=terminal`） |
| `FREEBUFF_HARDWARE_PRIVACY` | `auto` | 跨平台硬件虚拟化（`auto`、`macos`、`linux`、`off`） |
| `FREEBUFF_PROXY_LISTEN_IP` | `127.0.0.1` | Sing-box 入站的本地代理监听 IP |
| `FREEBUFF_PROXY_HOST` | `proxy.internal` | 传递给工作进程的虚拟主机名，用于 `px=remote` 判定 |
| `FREEBUFF_JITTER_MODE` | `human` | 击键模拟时序配置（`human`、`fast`、`instant`） |
| `FREEBUFF_LIVE_CATALOG` | `false` | 可选开启网关主机的直接目录/会话/`/me` 查询（默认离线；每个工作进程内的 CLI 已通过各自账户的代理获取实时目录） |

---

## 🧪 测试与验证

测试套件 100% 离线运行，使用 mock CLI 夹具与临时沙箱目录，不会提交提示词或消耗上游额度。CI 在每次推送时运行同一套件（`.github/workflows/ci.yml`）：

```bash
# 运行全部 339 项离线回归测试
python3 tests/run_offline.py
```

### 测试覆盖
- **终端颜色 OSC 应答（`test_terminal_colors.py`）**：覆盖 OSC 10/11 查询分片、BEL/ST 终止符、重复查询、串行写锁与延迟降低。
- **模型准入与致命弹窗（`test_admission_and_fatal_modals.py`）**：测试 `Enter: confirm and send` 自动确认、有界 `Enter: retry`，以及封禁、地域封锁、匿名网络、配额耗尽与 IP 上限弹窗的即时快速失败。
- **代理回环虚拟化与文件句柄上限（`test_proxy_loopback_and_ulimit.py`）**：测试 `RLIMIT_NOFILE` 软上限提升，以及 JS 预加载把回环代理虚拟化为 `proxy.internal`。
- **硬件隐私（`test_hardware_privacy.py`）**：测试合成 UUID/序列号的确定性、格式校验、平台策略执行与沙箱隔离。
- **私有构建与 PostHog 停用（`test_cli_build.py`、`test_posthog_opt_out.mjs`）**：验证 bundle SHA-256 完整性、回执校验与空操作客户端替换。
- **进程视图谱系（`test_process_view.py`）**：测试 macOS `ps` shim 与 Linux `prctl(PR_SET_NAME, "terminal")`。
- **Git 隐私屏蔽（`test_git_privacy.py`）**：验证 origin URL 的退出码 128 抑制与 status/diff/commit 的透传。
- **超长提示词附件（`test_response_attachments.py`）**：测试超过 2000 字符的提示词自动转换为原生 `textAttachments`。
- **工作进程生命周期与并发（`test_worker_lifecycle.py`、`test_worker_turns.py`、`test_multi_account_pool.py`）**：测试回合租约独占性、通过生命周期管道清理异常退出，以及最闲优先的负载分配。
