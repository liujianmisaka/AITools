# Multi-Agent Platform V2

V2 是面向本地编码 Agent 的持久化任务编排平台。它与 V1 完全隔离，不提供兼容路由或数据迁移层。

当前里程碑是 Phase 0/1：

- 独立 Python 工程与锁定依赖。
- Control API 只允许绑定回环地址。
- Temporal、PostgreSQL 和 Artifact Root 组件探针。
- `/live`、`/ready`、`/health/components`。
- SQLAlchemy 与 Alembic 空基线。
- 可重复的环境前置检查和 Fake 单元测试。

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

## 验证

```powershell
uv run ruff check .
uv run basedpyright
uv run pytest
```

本机基础设施集成测试默认跳过。只有 Temporal、PostgreSQL 和迁移均已显式启动时才运行：

```powershell
$env:MULTI_AGENT_V2_RUN_INFRA_TESTS = "1"
uv run pytest -m integration
```

架构设计见 [V2 架构设计](../multi-agent/docs/architecture-v2.md)。
