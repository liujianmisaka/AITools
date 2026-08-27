# Multi-Agent V3

V3 是破坏性重构版本，核心是独立的 Python Composition Kernel 和可替换 Capability Provider。

## 设计文档

V3 的目标架构和迁移基线统一维护在本目录下：

- [领域优先总体架构](docs/architecture-capability-first-v3.md)
- [Capability Seam 与 Provider Contract](docs/capability-seams-v3.md)
- [Invocation / Execution 生命周期](docs/invocation-lifecycle-v3.md)
- [Composition Kernel](docs/kernel-design-v3.md)
- [模块依赖矩阵](docs/module-dependency-matrix-v3.md)
- [Application Profile 目录](docs/profile-catalog-v3.md)
- [Delegation 与 A2A 独立方案](docs/a2a-standalone-v3.md)
- [Delegation、Continuation 与 Interaction Contract](docs/delegation-continuation-v3.md)
- [V3 实施阶段](docs/phase-capability-first-v3.md)
- [V3 Phase 0—10 完成审计](docs/v3-phase-0-10-completion-audit.md)
- [ADR-0002：Capability-First 架构决策](docs/adr/0002-capability-first-architecture.md)
- [当前实现迁移映射](docs/implementation-map-v3.md)

其中架构、Contract 和生命周期文档是规范基线；迁移映射只描述当前实现如何逐步迁移，不反向定义目标架构。

当前实现顺序：

1. Kernel/Invocation Contracts；
2. Composition Kernel；
3. Invocation Runtime；
4. Agent、A2A 和基础能力；
5. Coordinators；
6. Application Profiles。

当前已完成：

- Kernel/Invocation Contracts；
- Composition Kernel；
- Invocation Runtime；
- Agent Capability、Fake Agent Provider 和 Codex SDK Provider；
- Policy、Artifact、Session、Process 和 Workspace 基础能力；
- 不依赖 Workflow、Temporal、Control Plane 或 Web 的 agent-host Fake Profile。
- 复用 Agent Host 组合但使用独立 Composition Snapshot 的 standalone-agent Profile。
- 提供一次性与可持续调用、Interaction Channel 和明确事实所有权的 local-delegation Profile。
- 独立的 A2A Task Contracts、Memory Task Store 与 Invocation Runtime 桥接；
- 基于官方 `a2a-sdk` 的 Agent Card、JSON-RPC、REST、SSE 和客户端；
- 不依赖 Codex、Workflow、Temporal、PostgreSQL 或 Control Plane 的 a2a-node Profile。
- Provider-neutral 的 Direct、Reactive 和 Queue Coordinators；
- 可重放、可去重、可按 topic 订阅的内存 Event Source。

## Coordinator Runtime

`misaka-coordinator-runtime` 是对 `InvocationRuntime` 的独立编排层，不包含 Provider、A2A、
Workflow、Temporal 或数据库依赖：

- `DirectCoordinator`：提交一个显式的 `InvocationRequest`，并负责取消和有界停服；
- `ReactiveCoordinator`：订阅 `EventSource`，将事件通过路由工厂转换为 Invocation，支持 topic
  过滤、event_id 去重和并发上限；
- `QueueCoordinator`：提供有界队列、Job ID 幂等、worker 并发、失败/拒绝重试，以及对
  `reconciliation_required` 的人工处理边界；
- `MemoryEventSource`：为本地 Profile 和 Fake 测试提供单调 sequence、断线续读和 close 生命周期。

所有模型和推理等级仍由调用方在 `InvocationRequest` 中显式传入，Coordinator 不读取默认模型。

## Event Source 与 Decision Gate

`misaka-event-source-runtime` 独立提供 CloudEvents 1.0 信封、可重放 Memory Source、Webhook HMAC
准入、Git 分支 commit 轮询、Timer 和带时区的 Cron 计算；这些 Source 不依赖 Control Plane 或
Workflow，可以由 Reactive Coordinator、Control Plane Profile 或其他宿主消费。

`misaka-approval-capability` 定义一次性人工决定契约和 Memory Provider；
`misaka-approval-persistence-jsonl` 提供可重放的 JSONL Provider。Control Plane 只消费公开
`DecisionStore`，不再拥有决定状态机实现。

`misaka-tool-capability` 定义独立的工具发现和执行 Seam，并提供可注入 Kernel 的
`MemoryToolProvider`。工具调用拥有显式输入/输出 JSON Schema、幂等键和取消边界；Provider
不会读取 Workflow 或 Control Plane 状态。未来的本地进程工具、MCP 工具可以替换该 Provider，
而不改变调用方的 Tool Contract。

## Durable Persistence

`misaka-persistence-jsonl` 提供标准库实现的追加式 JSONL Event Log 和 Durable Job Registry：事件
按 stream 保持单调 sequence，重复 event_id 幂等，内容冲突拒绝；Job Registry 通过事件重放恢复，
使用 version CAS 防止并发覆盖。它适合本地单进程 Profile 和 Fake 测试。

`misaka-persistence-contracts` 定义 Provider-neutral 的 Event Store 与 Job Registry 契约；
`misaka-persistence-postgres` 使用 PostgreSQL 事务、stream advisory lock、唯一约束和 version CAS
提供多进程事实源。JSONL 与 PostgreSQL 是可替换 Profile 选择，不能同时推进同一个 Job 状态。

`misaka-coordinator-temporal` 是独立的 Temporal Coordinator：Temporal Workflow 是该 Profile 的
唯一执行事实源，Activity 只桥接 `InvocationRuntime`，并以 primitive DTO 穿过 Temporal JSON
边界。Activity 会持续 heartbeat，取消会先调用 Invocation handle 的 cancel；Temporal 不会被
JSONL/PostgreSQL Store 同时推进同一执行。

`misaka-coordinator-workflow` 是可选的上层 Coordinator，提供 DAG 和 State Machine 两种组合
方式。它只依赖 `InvocationRuntime`，可以被整个系统删除而不影响 Agent、A2A、Direct、Reactive、
Queue 或 Durable Profile；DAG 节点和状态转换仍要求调用方显式构造 `InvocationRequest`。

## Local Control Plane 与 Web V3

`misaka-profile-control-plane` 提供无登录、仅面向本机/局域网部署的 FastAPI API；它把 Job
事实写入可替换的 Durable Job Registry，不直接导入 Provider SDK。Fake 真实入口：

    uv run python examples/control_plane_fake.py

Codex Profile 开发入口：

    uv run python examples/control_plane_codex.py `
      --codex-home C:/Users/<user>/.codex `
      --port 8017

该入口注册 Codex Provider 和模型目录，不会在启动或 `/models` 请求时执行推理。Delegation 请求
必须通过顶层 `cwd` 显式提供一个存在的绝对目录；默认允许任意目录。需要限制范围时可重复传入
`--allowed-path-root D:/allowed/root`，Control Plane 会在调用 Provider 前规范化路径并强制筛选。
任务仍必须显式提供模型、推理等级和 `network_policy`；默认 `deny` 在没有宿主强制能力时会被
Provider 拒绝，允许联网必须由任务请求显式传入 `network_policy: "allow"`。
Codex Provider 的 Session Lease 在该本地示例中绑定进程内 Session Store；跨进程的 Durable Session
Store 仍需由部署 Profile 显式提供。

独立前端位于 D:/dev/AITools/multi-agent-web-v3，执行 npm install 后使用 npm run dev，
默认通过 Vite /api 代理访问 http://127.0.0.1:8016。页面包含执行中心、委派状态、能力目录、
任务创建弹窗和任务详情抽屉；委派状态通过 actor-aware 的 GET /delegations 读取应用投影并定时
刷新，不直接读取 JSONL 或 Provider 原始事件。模型目录由已注册 Provider 的公开目录接口提供，
提交任务时仍必须显式选择模型、Provider 和推理等级，不使用前端静态默认值。

Control Plane 重启恢复遵循安全边界：尚未开始外部调用的 `queued` 任务可以继续调度；重启时处于
`running` 的任务无法证明外部 Agent 是否已经启动，因此会收敛为 `reconciliation_required`，不会
自动重复启动可能产生副作用的 Agent。

委派详情页面会为 `reconciliation_required` 提供人工结算表单。操作者必须先核对实时或历史 Agent
会话，再提交当前 `revision`、幂等键、最终状态和核对依据；服务端只允许结算为 `completed`、
`failed` 或 `cancelled`，revision 已变化或任务本来不需要对账时拒绝改写。相同能力也通过
`POST /delegations/{delegation_id}/reconciliation/resolve` 提供。Provider 的操作标识在一次
Invocation 内保持不可变；供应商终态消息携带的结果 UUID 不会替换该标识并制造虚假的对账状态。

Control Plane 还提供模板/实例资源：

- `POST /templates` 保存不可变的模板版本；同一 `template_id + version` 只能保存一次；
- `POST /templates/{template_id}/instances` 从指定版本创建独立实例，实例持久化模板版本和状态；
- 模板可选择 `direct`（单节点）或 `dag`（节点依赖、输出结果按节点保存）；
- 服务重启时，运行中的实例与任务采用相同的 `reconciliation_required` 安全边界。

Control Plane 还提供静态服务目录和本地服务生命周期管理：

- `GET /services` 展示当前 Profile 支持的服务及实时状态；
- `POST /services/{service_id}/start` 启动已登记的本地服务；
- `POST /services/{service_id}/stop` 停止已登记的本地服务；
- 首批内置 `a2a-node` 和 `a2a-agent-host`，命令由 Profile 固定声明，前端不能提交任意命令；
- 服务目录在进程启动时确定，当前不支持热插拔或动态安装服务。

Web V3 的“服务管理”页面会自动读取该目录，展示 Control Plane 所有的 A2A 服务端点、PID、
最近日志和生命周期状态。该页面不负责启动 Control Plane 自身。

AITools 根目录还提供独立的 `multi-agent-service-web` 引导管理面。它包含默认监听 `8014` 的
Management API 和默认监听 `5174` 的页面，不依赖预先启动的 Control Plane。Management API
直接复用公开的 `misaka_service_runtime.ServiceManager` 托管 Control Plane 与主 Web，再通过
Control Plane HTTP API 合并并操作下游 A2A 服务。依赖方向始终是“AITools 外围管理面 -> V3
公共运行时与 Control Plane API”，V3 核心和 Control Plane 不反向依赖管理面。

统一平台通过 `examples/control_plane_multi.py` 组合单个 Control Plane：启动时按持久化列表创建并
注册一个或多个 Fake/Codex/Claude Provider。不同 Codex Provider 可以使用独立的 `codex_home`、
`config_overrides` 和网络隔离声明；Claude Provider 使用可选 `claude_config_dir`、`claude_cli_path`
和显式 `model_ids` 目录；如果只是同一 Provider 下的不同模型，则不需要复制 Provider，
调用方通过 `/models` 目录和任务级 `provider_id`、`model`、`effort` 完成选择。持久化的
`config_overrides` 只接受 Provider 选择、无凭据 endpoint 与环境变量名等安全引用，不保存密钥、
Header 或带凭据/查询参数的 URL。

统一目录还会把 MCP 网关标记为客户端按需启动的 stdio 进程，不把它伪装成常驻服务。所有
单服务启停仍携带页面所见的当前 epoch；停止 Control Plane 时先校验 epoch，再停止下游 A2A
服务和主 Web。页面不接受任意命令、工作目录、环境变量或进程参数。

事件触发接口使用版本化 `event_type` 和调用方提供的 `event_id`：

- `POST /triggers` 注册事件类型到模板版本的绑定；
- `POST /events` 接收事件并按 `trigger_id + event_id` 去重；
- 同一事件重复提交只返回已创建的实例，不会启动第二次 Agent；
- 当前接口只负责事件准入、持久化投递和实例创建，Git Poller、Webhook Server、Cron 等 Event Source
  仍作为独立能力接入，不把定时器或外部监听器写进 Control Plane 核心。

### 事件触发委派会话

不需要 Workflow 模板的事件源统一调用 `POST /delegations/trigger`。该接口只负责把一个通用事件
映射为标准 `DelegationSubmission`，随后复用与 `POST /delegations` 完全相同的路径筛选、Provider
路由、Decision Gate、持久化、Session 和可视化链路。Control Plane 不在接口内部运行 Webhook
Server、Git Poller、Cron 或消息队列消费者。

~~~powershell
$body = @'
{
  "trigger_id": "repository-review",
  "event": {
    "event_id": "push-20260824-1",
    "source": "git.example/repository",
    "event_type": "dev.repository.push.v1",
    "subject": "refs/heads/main",
    "occurred_at": "2026-08-24T10:00:00Z",
    "data": {
      "repository": "example/project",
      "ref": "refs/heads/main",
      "commit": "0123456789abcdef0123456789abcdef01234567"
    }
  },
  "delegation": {
    "actor": {"principal_id": "event-router", "kind": "application"},
    "initiator": {"principal_id": "event-router", "kind": "application"},
    "controller": {"principal_id": "event-router", "kind": "application"},
    "scope": {"scope_id": "repository-review"},
    "capability_id": "agent.invocation",
    "operation": "invoke",
    "input": {"prompt": "检查本次仓库事件并报告风险。"},
    "cwd": "D:/dev/project",
    "provider_id": "codex",
    "model": "pixel/gpt-5.6-luna",
    "effort": "high",
    "policy_context": {"sandbox": "read_only", "network_policy": "deny"},
    "output_schema": null,
    "plan_hash": "0000000000000000000000000000000000000000000000000000000000000000",
    "mode": "continuable",
    "decision_ref": null
  }
}
'@
Invoke-RestMethod `
  -Method Post `
  -Uri http://127.0.0.1:8016/delegations/trigger `
  -ContentType application/json `
  -Body $body
~~~

示例中的全零 `plan_hash` 只用于展示字段格式，真实调用必须替换为当前委托计划的 SHA-256。
接口立即返回现有 `DelegationView`，委派继续异步执行，并自动出现在 `GET /delegations`、Web V3
委派列表和会话事件流中。事件身份由 `trigger_id + event.source + event.event_id` 组成：完全相同的
请求可以安全重试且不会启动第二个 Agent；同一身份携带不同事件内容或委托规格时返回 `409`，
要求调用方显式修正冲突。不同 `trigger_id` 可以独立消费同一外部事件。

### 委派会话消息调度

`POST /delegations/{delegation_id}/messages/dispatch` 是委托者与被委托 Agent 的统一执行消息入口。
它只接受 `continuable` Delegation，并持久化每次 Dispatch 的幂等键、状态、应用策略和前后
Activation ID：

~~~json
{
  "dispatch_id": "dispatch-1",
  "idempotency_key": "dispatch-key-1",
  "actor": {"principal_id": "control-client", "kind": "application"},
  "session_id": "session-1",
  "expected_activation_id": "activation-1",
  "delivery": "append",
  "message_id": "message-1",
  "message_type": "instruction",
  "payload": {"prompt": "补充检查测试覆盖率。"},
  "model": null,
  "effort": null
}
~~~

- 活动 Activation 上的 `append` 需要 `expected_activation_id`。Provider 支持 steering 时直接实时
  输入，否则消息进入持久队列，在当前 Activation 结束后继续；
- 已结束会话上的 `append` 在同一 Provider Session 新建 Activation，可省略 Activation 栅栏；
- `interrupt_continue` 只允许用于活动 Activation：先确认打断终态，再以同一 Session 继续；
- `model` 与 `effort` 必须成对提供。活动会话指定新执行选择时不会热改当前模型，而是排队到新的
  Activation；
- `answer` 消息必须携带 `reply_to` 与 `correlation_id`，用于回答 Agent 已发布的 question；
- HTTP 调用方不能提交 `cwd` 或 `sandbox`。Control Plane 从原 Delegation 的可信 Gateway 元数据
  恢复执行上下文，并再次执行当前允许路径策略，路径已被撤销时拒绝继续；
- 打断或外部消息投递无法确认时，Dispatch 进入 `reconciliation_required`，不会猜测成功或重复
  启动 Agent。

Runtime 同时把委托者输入投影为 `input_message` 会话事件，前端不需要从 Provider 私有日志反推
输入内容。Provider 可以用公开事件 `agent.question` 发布可回答问题；Runtime 会把它同时写入
Session Event 和 Interaction Channel，保留 `question_id`、选项和关联 ID。该桥接只在拥有开放
Interaction Channel 的 `continuable` 委托中形成可回答消息；一次性任务仍只把问题作为会话观察
事件显示，不伪造可继续的对话状态。

交互式调用建议使用独立 `multi-agent-mcp` 中的 `send_task_message`；该工具只发送 instruction，
Agent 问答和 Web 双向消息仍使用同一 Control Plane 合同。`wait_task` 只做有界等待，不会取消或
推进 Dispatch。

Provider 收到的输入会在原 `input` 基础上增加只读的 `trigger_event` 字段，调用方不能自行覆盖。
事件数据仍执行 Gateway 的 JSON、凭据字段和执行上下文安全校验。`cwd`、Provider、模型、effort、
沙箱和网络策略继续由每次委托显式提供，不保存全局 WorkspaceRoot。事件监听、签名校验、断线重试
和投递游标属于外部 Event Source/Adapter；本阶段不在 Control Plane 中增加常驻触发器。

需要人工准入的模板可以设置 `decision_required: true`：实例会进入 `waiting_decision`，
通过 `POST /decisions/{proposal_id}/revisions/{revision}/decision` 写入一次性
approved/rejected 决定；决定事实和实例状态都从 Durable Log 恢复，重复决定不会覆盖已提交的不同决定。

DAG 不是 Control Plane 的硬依赖。需要 DAG 的 Profile 显式安装并注入
`misaka-profile-control-plane-workflow` 提供的 `create_dag_runner(runtime)`；未注入时，DAG
实例会被拒绝并进入对账状态，避免 Control Plane 偷偷引入 Workflow 执行事实源。

在仓库根目录只启动 AITools Management API 和服务管理页面：

    .\start-multi-agent-service-web.ps1

打开 http://127.0.0.1:5174 后，先保存 Provider 列表、各 Provider 的运行配置、网络策略和可选路径筛选，
再选择“启动核心”或“启动全部”。也可以使用上次已保存的配置一次启动管理面、Control Plane 和
Web V3：

    .\start-multi-agent-v3-dev.ps1

停止 Control Plane、Web V3 及依赖它们的下游服务，但保留管理面：

    .\stop-multi-agent-v3-dev.ps1

停止全部业务服务以及管理页面和 Management API：

    .\stop-multi-agent-service-web.ps1

真实 Codex Provider 不再通过启动脚本传入固定 Workspace。统一平台在
`.data/aitools-service-manager/configuration.json` 保存运行配置；允许路径列表为空时接受任意存在的
绝对目录，配置一个或多个根路径时由 Control Plane 在每次 Delegation 前强制筛选。配置只能在
Control Plane 停止时修改。旧版单 Profile 或 version 2 配置会在管理面首次加载时原子迁移到 version 3 的
`providers[]` 结构；已有的唯一 `control-plane-codex.jsonl` 或 `control-plane-fake.jsonl` 会继续使用，
不会因为 Provider 组合改变而隐藏历史。

## Standalone A2A 真实入口

先启动只绑定回环地址的独立 A2A 节点：

    uv run python -m misaka_a2a_node --host 127.0.0.1 --port 8015

另开一个终端，使用官方 A2A SDK 客户端完成 Agent Card 发现、流式任务调用和查询：

    uv run python examples/a2a_client_smoke.py --base-url http://127.0.0.1:8015

节点公开：

- `/.well-known/agent-card.json`：官方 Agent Card；
- `/a2a`：A2A 1.0 JSON-RPC 与 SSE；
- `/a2a/message:send`、`/a2a/message:stream`、`/a2a/tasks/*`：官方 REST/SSE；
- `/health`：本地进程健康状态。

组合 Profile 也可以直接把同一个 Agent Host 暴露为 A2A 服务：

    uv run python -m misaka_a2a_agent_host --host 127.0.0.1 --port 8016

该 Profile 只组合 Agent Host、A2A Task Handler 和 HTTP/SSE Transport；它不引入 Workflow、
Control Plane 或 Temporal，适合验证“本地 Agent 能力通过 A2A 发布”的最小闭环。

`misaka-durable-agent-profile` 用 Temporal History 作为 Invocation 的执行事实源，
PostgreSQL 仅记录可重放的接受、启动、取消请求和终态审计事件，不双写执行状态。
真实部署通过 `DurableAgentProfile.from_temporal(...)` 注入 Temporal Client、PostgreSQL Store
和 Agent Host；测试可以注入 Fake Coordinator/Worker，不需要启动 Temporal。

任务必须通过 Message metadata 显式传入 `capabilityId`、`operation`、`model` 和
`effort`；Profile 不提供默认模型。断线续订可发送 `X-A2A-Start-Sequence`，事件的
`metadata.sequence` 用于客户端去重。绑定 `0.0.0.0` 或 `::` 时必须显式提供
`--public-url`，避免 Agent Card 发布不可访问的通配地址。

Codex Provider 使用独立的 `provider-codex` 包。真实调用必须显式提供模型、推理等级、工作目录和
沙箱类型；Provider 不读取默认模型。可选的目录筛选属于 Control Plane/Application Profile，Provider
只校验收到的工作目录为存在的绝对目录。模型目录通过短生命周期 Codex SDK 客户端显式读取，不会
在 `describe()` 中启动真实 API 调用。

## Codex 真实烟测

烟测脚本具有 Provider 启动、取消和整体执行截止时间，不会无限等待。必须显式传入模型、
effort、工作目录和实际使用的 Codex Home：

    uv run python -u examples/codex_smoke.py `
      --model pixel/gpt-5.6-luna `
      --effort high `
      --cwd D:/dev/AITools/multi-agent-v3 `
      --codex-home C:/Users/<user>/.codex `
      --timeout-seconds 60 `
      --rpc-timeout-seconds 20 `
      --allow-network `
      --ephemeral

`--allow-network` 会允许 Codex 进程联网，本烟测用它访问本地 OpenCodex 代理；它不等价于
“仅允许 loopback”。任务文件系统沙箱仍为 `read_only`。正式 Profile 必须显式提供 Codex
Home，并由平台网络策略实施 loopback/deny 边界，不能依赖进程默认用户目录。

V3 不导入 multi-agent-v2，不保留 V2 API、数据库模型或兼容层。

## 开发验证

在 `multi-agent-v3` 目录执行：

    $env:UV_CACHE_DIR = "D:/dev/AITools/multi-agent-v3/.tmp-uv-cache"
    uv sync --all-packages
    uv run pytest -q
    uv run ruff check .
    uv run ruff format --check .
    uv run basedpyright -p pyproject.toml
    uv run python tools/check_import_boundaries.py

真实 PostgreSQL/Temporal 验收使用 V3 自有的 `deploy/local/compose.yaml` 拓扑。复制
`deploy/local/.env.example` 的变量并启动服务后，显式传入以下两个测试变量：

    docker compose --env-file deploy/local/.env -f deploy/local/compose.yaml up -d
    $env:MULTI_AGENT_V3_POSTGRES_DSN = "postgresql://multi_agent_v3_app:<password>@127.0.0.1:5432/multi_agent_v3"
    $env:MULTI_AGENT_V3_TEMPORAL_TARGET = "127.0.0.1:7233"
    uv run pytest -q

测试结束后必须关闭临时容器和卷：

    docker compose --env-file deploy/local/.env -f deploy/local/compose.yaml down -v

未配置这两个测试变量时，Temporal/PostgreSQL 测试会安全跳过。
