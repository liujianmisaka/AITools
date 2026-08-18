# Multi-Agent V3

V3 是破坏性重构版本，核心是独立的 Python Composition Kernel 和可替换 Capability Provider。

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

任务必须通过 Message metadata 显式传入 `capabilityId`、`operation`、`model` 和
`effort`；Profile 不提供默认模型。断线续订可发送 `X-A2A-Start-Sequence`，事件的
`metadata.sequence` 用于客户端去重。绑定 `0.0.0.0` 或 `::` 时必须显式提供
`--public-url`，避免 Agent Card 发布不可访问的通配地址。

Codex Provider 使用独立的 `provider-codex` 包。真实调用必须显式提供模型、推理等级、
工作目录和沙箱类型；Provider 不读取默认模型，并要求服务端配置工作区白名单。模型目录
通过短生命周期 Codex SDK 客户端显式读取，不会在 `describe()` 中启动真实 API 调用。

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
