# Multi-Agent Platform V2

V2 是面向本地编码 Agent 的持久化任务编排平台。它与 V1 完全隔离，不提供兼容路由或数据迁移层。

当前已实现 Phase 1–6：

- 独立 Python 工程与锁定依赖。
- Control API 只允许绑定回环地址。
- Temporal、PostgreSQL 和 Artifact Root 组件探针。
- `/live`、`/ready`、`/health/components`。
- SQLAlchemy/Alembic、execution lease、heartbeat、reconcile 和 worktree fencing。
- 严格 Workflow DSL、immutable IR、DAG/状态机和 Temporal Workflow Runtime。
- FakeRuntime、CodexRuntime 与独立 Windows Agent Worker。
- 模板/版本、实例投影、审批、审计、CloudEvents Inbox 和命令 Outbox。
- Generic Webhook HMAC、Git 分支 commit 检测、Temporal Schedule 和耐久 `wait_event`。
- 独立 Orchestration Worker、Command Dispatcher 和 Provider Catalog Refresher。
- 可重复的环境前置检查、Fake 契约测试与 Temporal replay 测试。
- 独立 React/Web BFF、SSE 恢复、局域网监听边界与受监督启停脚本。
- 容量、依赖锁、SBOM、Secret、V1 切换和基础设施集成验收入口。

## 环境

- Python 3.12
- `uv`
- Temporal CLI
- PostgreSQL 16
- Docker Desktop 或其他提供 `docker compose` 的本机运行时

安装依赖：

```powershell
uv sync --all-groups
```

执行前置检查：

```powershell
uv run multi-agent-v2-preflight
```

快速启动开发 Temporal Service。`--db-filename` 只用于单元和开发迭代，不能作为正式运行拓扑：

```powershell
temporal server start-dev --ip 127.0.0.1 --db-filename .data/temporal.db
```

持久化集成与最终运行使用 PostgreSQL-backed Temporal。为避免把本机密码文件误加入 Git，直接在当前终端设置环境变量后启动：

```powershell
$env:MULTI_AGENT_V2_POSTGRES_ADMIN_PASSWORD = "replace-with-bootstrap-password"
$env:MULTI_AGENT_V2_TEMPORAL_DB_PASSWORD = "replace-with-temporal-runtime-password"
$env:MULTI_AGENT_V2_CONTROL_DB_PASSWORD = "replace-with-control-app-password"
$env:MULTI_AGENT_V2_DATABASE_URL = "postgresql+asyncpg://multi_agent_app:$env:MULTI_AGENT_V2_CONTROL_DB_PASSWORD@127.0.0.1:5432/multi_agent_v2"
$env:MULTI_AGENT_V2_TEMPORAL_ADDRESS = "127.0.0.1:7233"
docker compose -f deploy/local/compose.yaml up -d
uv run alembic upgrade head
```

Compose 只将 PostgreSQL 和 Temporal 绑定到 `127.0.0.1`；它不暴露 Temporal UI 或任何局域网端口。

启动 Control API：

```powershell
uv run multi-agent-v2-api
```

默认地址为 `http://127.0.0.1:8011`。Control API 不允许监听局域网地址；后续只有 Web/BFF 可以对局域网开放。

启动 Phase 4 后台组件：

```powershell
uv run multi-agent-v2-orchestration-worker
uv run multi-agent-v2-dispatcher
uv run multi-agent-v2-catalog-refresher
```

这些进程都只连接本机 PostgreSQL、Temporal 或本地 Agent Runtime，不监听局域网端口。
模板创建、实例启动、Trigger、Schedule 和外部事件通过 `/api/v2` Control API 管理；
跨 PostgreSQL/Temporal 的启动、Signal 和 Schedule 同步均经持久化 Outbox 投递。

## Generic Webhook

默认要求 HMAC。每次请求必须携带唯一 nonce，并将时间戳、Webhook 来源名、nonce 和原始
body 一并签名，避免同一签名被换来源或换事件 ID 重放：

```text
X-Misaka-Timestamp: <Unix seconds>
X-Misaka-Nonce: <unique delivery id>
X-Misaka-Signature: sha256=<hex HMAC-SHA256>

signed bytes =
  timestamp + "\n" + source_name + "\n" + nonce + "\n" + raw_body
```

请求地址为 `/api/v2/webhooks/{source_name}`。nonce 同时作为 CloudEvent ID，
PostgreSQL Inbox 再按 `source + id` 去重。请求体在解析前执行流式大小限制。

## Agent Worker

Agent Worker 只接受服务端登记的 workspace ID。先创建本机配置文件，例如
`.data/workspaces.json`：

```json
{
  "workspaces": [
    {
      "id": "repo",
      "root": "D:/dev/project",
      "worktreeRoot": "D:/dev/.multi-agent-worktrees",
      "baseRef": "HEAD"
    }
  ]
}
```

路径可写为绝对路径，也可相对于配置文件所在目录。启动 Worker：

```powershell
$env:MULTI_AGENT_V2_WORKSPACE_CONFIG_PATH = ".data/workspaces.json"
uv run multi-agent-v2-agent-worker
```

任务必须显式传入 provider、model 和 effort。Codex 的 `networkPolicy=deny`
仅在平台提供受限运行环境并设置
`MULTI_AGENT_V2_CODEX_NETWORK_DENY_ENFORCED=true` 时准入；Worker 同时向每个
Codex CLI 实例传入禁用 web search 和 sandbox 命令网络的覆盖项。未启用该平台
保障时任务会明确失败，不会静默放宽策略。

## 验证

```powershell
uv run ruff check .
uv run basedpyright
uv run pytest
```

从仓库根目录运行完整的 Fake、安全、前端、SBOM 和容量验收：

```powershell
.\verify-multi-agent-v2.ps1 -RunCapacity
```

本机基础设施集成测试默认跳过。只有 Temporal、PostgreSQL 和迁移均已显式启动时才运行：

```powershell
$env:MULTI_AGENT_V2_RUN_INFRA_TESTS = "1"
uv run pytest -m integration

# 或从仓库根目录连同其他验收一起执行
.\verify-multi-agent-v2.ps1 -RunInfrastructure -RunCapacity
```

架构设计见 [V2 架构设计](docs/architecture-v2.md)。
最近一次 Phase 6 验收记录见 [Phase 6 验收记录](docs/phase6-acceptance.md)。
