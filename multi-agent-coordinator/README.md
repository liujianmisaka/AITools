# Multi-Agent Coordinator

`multi-agent-coordinator` 是 AITools 层的独立协调 Agent 服务。它使用 Microsoft Agent
Framework 负责持续对话、计划和工具调用，通过 Multi-Agent V3 的公开 MCP/HTTP 接口派遣任务。

本项目不能导入 V3 Provider、Invocation Runtime、持久化实现或 Control Plane 内部模块。V3
继续拥有 Delegation、Activation、Invocation、Worker Session、权限、取消和对账等执行事实。

## 已建立的基线

阶段 0 建立依赖隔离和技术基线：

- 使用选择性 MAF 包，不安装完整 `agent-framework` 元包；
- 验证 `AgentSession` 可以序列化并恢复；
- 验证 OpenAI-compatible 客户端可以配置自定义 `base_url`；
- 验证 MCP stdio、Streamable HTTP 和 WorkflowBuilder 接口可用；
- 不连接模型、不启动 MCP 子进程、不产生推理调用。

架构边界见 [ADR-0001](docs/adr-0001-agent-framework-boundary.md)。

阶段 1 建立纯标准库领域层：

- `CoordinatorSession` 关联认知会话、当前 Goal、Plan 和事件游标；
- Plan/PlanNode 使用显式状态转换和单调 revision；
- `ExecutionReference` 只保存 V3 执行 ID，不复制执行状态或结果；
- 会话使用严格版本化 JSON 序列化和恢复；
- MAF、V3、Provider SDK 和基础设施依赖不能进入领域层。

领域状态边界见 [ADR-0002](docs/adr-0002-coordinator-domain.md)。

阶段 2 建立 MAF Coordinator Agent：

- 支持 OpenAI 与自定义 OpenAI-compatible `base_url`；
- Provider 模型、Reasoning Effort、系统提示、输出 Token 和请求超时均通过配置传入；
- 使用严格 JSON Schema 生成 `CoordinatorDecision`，非法响应不会推进 decision step；
- 每次激活限制最大 decision step，避免无界认知循环；
- 固定 `store=False`，由 MAF 将本地消息历史写入可序列化的 `AgentSession`；
- 模型连接失败和超时转换为稳定的 Coordinator 应用错误。

MAF 会话决策见 [ADR-0003](docs/adr-0003-maf-agent-session.md)。

## 开发验证

```powershell
uv sync --all-groups
uv run misaka-coordinator-baseline
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
uv run basedpyright -p pyproject.toml
```

技术基线命令只构造本地对象并输出版本及能力，不读取凭据，也不会访问网络。
