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

阶段 3 建立 MCP Tool Registry：

- MAF stdio/Streamable HTTP MCP 通过可替换 `ToolSource` 接入；
- MCP Source 与 Registry 分别执行原始名称、暴露名称白名单；
- 工具能力由配置标签分类，不在 Coordinator 中硬编码 V3 工具名；
- 发现快照支持单来源故障降级和同名冲突拒绝；
- 调用前执行 JSON Schema 校验，并统一处理超时和来源错误；
- 给 MAF Agent 的 FunctionTool 是 Registry 代理，不会绕过校验、超时和审计；
- 审计不保存参数值和工具结果。

工具边界见 [ADR-0004](docs/adr-0004-mcp-tool-registry.md)。

阶段 4 建立 V3 执行适配器：

- V3ExecutionGateway 将 Coordinator 请求映射到 V3 MCP 的委派、查询、等待、列表、消息、取消、
  对账和执行选项工具；
- 所有 V3 响应先解析为稳定的不可变契约对象，工具不可用、调用失败和协议错误分别归一化；
- V3SessionGateway 通过公开 HTTP API 读取历史会话快照/事件，并通过 SSE 订阅实时会话事件；
- 会话观察校验 delegation_id、单调序号、事件 envelope 和路径段 URL 编码，不依赖 V3 内部实现；
- 等待超时、列表数量、事件游标等边界在 Coordinator 侧拒绝非法值。

执行适配边界见 [ADR-0005](docs/adr-0005-v3-execution-adapter.md)。

## 执行适配器使用方式

执行适配器不自行启动 Control Plane，也不读取 Provider 凭据。应用层负责先构造 MCP Registry，再将
Registry 作为工具调用器注入：

~~~python
from misaka_coordinator_service.execution import (
    V3ExecutionGateway,
    V3SessionGateway,
    V3SessionGatewayConfig,
)

execution = V3ExecutionGateway(tools=registry)
sessions = V3SessionGateway(
    config=V3SessionGatewayConfig(
        control_plane_url="http://127.0.0.1:8016",
        actor_id="coordinator",
    )
)
~~~

常用操作对应关系如下：

| Coordinator 方法 | V3 MCP 工具 |
| --- | --- |
| delegate | delegate_task |
| get / wait / list | get_task_status / wait_task / list_tasks |
| send_message | send_task_message |
| cancel | cancel_task |
| resolve_reconciliation | resolve_task_reconciliation |
| execution_options | list_execution_options |

会话历史使用 sessions.get_session(delegation_id) 和 sessions.list_events(delegation_id)；实时观察
使用 sessions.stream_events(delegation_id, next_sequence=...)。应用层应在退出时调用 aclose()。

阶段 5 建立 PlanGraph 任务图：

- PlanGraph 以不可变依赖边描述 Coordinator 计划中的 DAG，不复制 V3 Delegation 或 Provider 状态；
- 依赖边加入和反序列化时拒绝自依赖、重复边和环，绑定 Plan 时校验节点引用；
- topological_order 提供稳定拓扑顺序，ready_node_ids 提供所有前置节点已 ACCEPTED 的 READY 节点；
- 图拥有独立 revision、时间戳和 JSON 序列化，具体持久化组合留在应用层决定。

任务图边界见 [ADR-0006](docs/adr-0006-plan-graph.md)。

阶段 6 建立 Coordinator 决策执行闭环：

- CoordinatorOrchestrator 在 MAF Agent 的 max_decision_steps 内循环读取事实并应用一个个结构化决策；
- CREATE_PLAN 创建 PlanGraph，DELEGATE 只通过 V3ExecutionGateway 创建委派，避免认知层直接操作 Provider；
- 只有满足依赖的 ready_node_ids 才能派遣，独立节点可以在一次 activation 内连续启动；
- WAIT 使用有界超时，完成、失败、取消和需要对账的 V3 快照会映射到 Coordinator 节点状态；
- activation 返回新的 CoordinatorSession 和 MAF AgentSession，由应用层负责保存和恢复。

应用层闭环边界见 [ADR-0007](docs/adr-0007-coordinator-orchestration.md)。

阶段 7 建立持续交互与对账入口：

- send_message 使用现有节点的 Worker Session 追加消息，不重新创建 Delegation；
- continue_node 使用 interrupt_continue 支持打断后继续；
- cancel_node 调用 V3 cancel_task 并同步节点状态；
- reconcile_node 调用 V3 resolve_task_reconciliation，完成后仍保留 REVIEW_REQUIRED，避免自动伪造人工接受；
- Invocation 结束后仍保留 Worker Session ID，支持历史会话继续交互。

持续交互边界见 [ADR-0008](docs/adr-0008-coordinator-continuation.md)。

阶段 8 建立委托事件触发与恢复：

- `CoordinatorSession.event_cursors` 按 delegation 保存下一个 V3 session event sequence，并参与 JSON
  持久化；重复事件不会重复推进游标，缺失序号会被拒绝。
- `CoordinatorEventBridge` 先重放历史事件，再从当前游标订阅 SSE；连接中断后有限次重放和重连，避免
  长会话因网络抖动丢失事件。
- SSE snapshot 会通过可选的 snapshot observer 同步节点执行事实；事件会映射为 CoordinatorEvent，
  不复制 V3 Provider 的内部状态。
- 输出、终态、等待输入和需要对账的事件会标记 `activation_required`，应用层可调用
  `CoordinatorEventBridge.activate()` 触发新一轮有界决策。

事件恢复示例：

~~~python
from misaka_coordinator_service.application import CoordinatorEventBridge

bridge = CoordinatorEventBridge(source=sessions, snapshot_observer=orchestrator)
while True:
    restart_stream = False
    async for update in bridge.consume(
        coordinator_session,
        delegation_id="delegation-1",
        node_id="task-1",
        at=utc_now(),
    ):
        coordinator_session = update.session
        persist(coordinator_session)
        if update.activation_required:
            result = await bridge.activate(
                update,
                orchestrator=orchestrator,
                prompt="继续完成当前目标",
                agent_session=agent_session,
                activation_id=new_activation_id(),
                at=utc_now(),
            )
            if result is not None:
                coordinator_session = result.session
                agent_session = result.agent_session
                persist(coordinator_session, agent_session)
                restart_stream = True
                break
    if not restart_stream:
        break
~~~

事件桥接只负责读取、校验和映射。应用层必须在 update 及 activation 返回后持久化会话；进程恢复时
重新传入保存的 `CoordinatorSession`，桥接会从对应 delegation 的 `event_cursor` 继续。
如果 activation 改变了计划或节点状态，应停止当前消费循环，并使用 activation 返回的新会话重新订阅；
这样不会用旧的计划状态覆盖 activation 的结果。

事件恢复边界见 [ADR-0009](docs/adr-0009-coordinator-event-recovery.md)。

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
