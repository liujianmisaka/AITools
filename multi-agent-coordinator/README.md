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

受管 Host 不把 V3 控制工具直接交给认知 Agent。Agent 只输出结构化 Coordinator 决策，
Orchestrator 再通过 Registry 和 V3ExecutionGateway 执行；这样自主性预算、权限和审批门禁不能被
模型直接调用 MCP 工具绕过。Registry 的 FunctionTool 代理保留给后续经过风险分类的非执行工具组。

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

计划修订使用稳定的 `plan_id` 和追加式 `PlanRevision` 历史。已绑定 V3 执行引用的节点不可被改写，
未执行分支可以新增、删除或重新选择；修订只更新 Coordinator 的认知计划，不修改已经发生的
Delegation/Activation/Invocation 事实。

阶段 6 建立 Coordinator 决策执行闭环：

- CoordinatorOrchestrator 在 MAF Agent 的 max_decision_steps 内循环读取事实并应用一个个结构化决策；
- CREATE_PLAN 创建 PlanGraph，DELEGATE 只通过 V3ExecutionGateway 创建委派，避免认知层直接操作 Provider；
- 只有满足依赖的 ready_node_ids 才能派遣，独立节点可以在一次 activation 内连续启动；
- WAIT 使用有界超时，完成、失败、取消和需要对账的 V3 快照会映射到 Coordinator 节点状态；
- activation 返回新的 CoordinatorSession 和 MAF AgentSession，由应用层负责保存和恢复。
- 支持 `dispatch_ready_nodes` 在一次激活中派遣所有满足依赖的独立节点；
- 支持 `revise_plan`、`accept_result`、`complete_goal`、`send_message` 和
  `cancel_delegation`，形成从规划、执行、验收到目标完成的闭环；
- 一个拓扑阶段全部验收后，应用服务会在后台触发下一阶段的 Coordinator activation；服务重启时
  也会恢复尚未启动的已解锁阶段；单次触发只执行一次有界决策，不在进程内无限重试；
- 接受最后一个节点后确定性完成 Plan 和 Goal，不要求模型再生成一次完成决策；
- `PlanRevision` 历史和目标修订保持可查询、可恢复。

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
- 输出、终态、Agent 提问、等待输入和需要对账的事件会标记 `activation_required`，应用层可调用
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
直接使用 `CoordinatorService` 或受管 Host 时，上述持久化、重订阅和失败重试由后台监督任务自动完成，
调用方不需要自行维护这段消费循环。

事件恢复边界见 [ADR-0009](docs/adr-0009-coordinator-event-recovery.md)。

阶段 9 建立可运行的应用服务与传输入口：

- `CoordinatorService` 统一管理激活、消息、继续、取消和人工对账，并按会话串行化并发操作；
- 取消会话时先逐个取消活动 V3 Delegation，再同步终止 Plan 和 Goal；任一取消失败时保留 Goal
  为活动状态，允许调用方重试；
- 归档以服务端 `archivable` 和 `archive_blocker` 为唯一判断来源；没有活动 Delegation 时，
  服务可在归档过程中修复旧记录中已终止 Goal 与非终态 Plan 的不一致；
- Coordinator 领域会话和 MAF `AgentSession` 通过追加式 JSONL 一起持久化，存储版本使用独立 CAS；
- `CoordinatorService.start()` 会恢复历史会话，为已有 Delegation 启动独立事件监督任务；游标先持久化，
  需要触发决策的事件同时写入待处理标记，模型失败或进程重启后使用同一 activation ID 重试；
- 请求级 `cwd` 与会话一起持久化，自动事件激活继续使用原工作目录；旧记录没有工作目录时监督任务
  不消费事件，并提示调用方先手工激活一次以补全路径；
- HTTP Host 提供健康探针、会话查询和应用操作 API，MCP Host 暴露同一组 Coordinator 工具；
- Host 只通过独立的 V3 stdio MCP 网关访问 Control Plane，不导入 V3 内部运行时或 Provider；
- 每次激活必须传入 `cwd`，允许由调用方选择任意工作目录，路径许可仍由 V3 Control Plane 执行。

本地直接启动 HTTP Host：

~~~powershell
uv run python -m misaka_coordinator_service.transport `
  --transport http `
  --control-plane-url http://127.0.0.1:8016 `
  --state-path ..\.data\multi-agent-coordinator\sessions.jsonl `
  --model pixel/gpt-5.6-luna `
  --reasoning-effort medium `
  --api-key-env OPENAI_API_KEY `
  --base-url http://127.0.0.1:10100/v1 `
  --port 8020
~~~

生产式本地使用应由 `multi-agent-service-web` 统一管理 Coordinator 和 Control Plane 的依赖顺序，
不需要单独运行上述命令。受管 HTTP Host 同时提供 REST API 和
`http://127.0.0.1:8020/mcp` Streamable HTTP MCP；默认本机 OpenCodex 地址在
`OPENAI_API_KEY` 未设置时使用固定本机令牌 `opencodex-proxy`，自定义地址仍要求宿主提供真实环境变量。

事件监督状态可通过 `GET /monitors` 或 MCP 工具 `coordinator_list_monitors` 查询。状态包含
Coordinator session、计划节点、Delegation、是否运行以及最近一次错误。Host 关闭时会先取消并等待所有
监督任务，再关闭 V3 Session Gateway 与 MCP Registry，不遗留后台 HTTP/SSE 客户端。

阶段 10 建立自主性预算与审批门禁：

- 并行委派、委派总数、子委派深度、计划修订、节点重试、Coordinator 累计模型运行时间和模型
  激活次数均由确定性策略限制；等待委派、人工验收和会话闲置不消耗运行时间预算；
- 用量、待审批、审批结果和授权消费状态随 Coordinator Session 持久化；
- Provider 或工作目录作用域扩大、工作区写入、破坏性操作、预算超限和人工对账需要外部批准；
- HTTP `POST /sessions/{session_id}/approvals/{approval_id}` 和 MCP
  `coordinator_resolve_approval` 是唯一审批入口，模型不能自行放宽权限；
- V3 控制工具不会直接暴露给认知 Agent，所有委派仍由 Orchestrator 经过策略检查后执行；
- 工具审计写入与会话文件同目录的 `sessions.tool-audit.jsonl`，可通过 `GET /tool-audits` 或 MCP
  `coordinator_list_tool_audits` 查询，审计不保存参数值和工具结果。

自主性边界见 [ADR-0010](docs/adr-0010-coordinator-autonomy.md)。

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
## Coordinator 业务 HTTP API

业务页面使用 /coordinator/* 规范路径，不需要导入 Coordinator Python 包。

- POST /coordinator/sessions 创建并激活一个持续 Coordinator 会话。
- GET /coordinator/sessions 及 GET /coordinator/sessions/{id} 查询持久会话。
- 会话列表摘要返回 `archivable` 和 `archive_blocker`；归档与恢复使用
  POST /coordinator/sessions/{id}/archive 和 POST /coordinator/sessions/{id}/unarchive。
- GET /coordinator/sessions/{id}/plan 查询 Plan、PlanGraph 和修订历史。
- POST /coordinator/sessions/{id}/messages 追加一轮用户消息；工作目录从会话恢复。
- POST /coordinator/sessions/{id}/cancel 取消当前目标；审批使用 /coordinator/sessions/{id}/approvals/{approval_id}。
- GET /coordinator/sessions/{id}/events 和 GET /coordinator/sessions/{id}/stream 按游标重放或订阅 SSE 事件。

Coordinator 事件日志独立保存为 sessions.events.jsonl，不复制 V3 Provider 私有状态；事件只包含用户消息、激活生命周期、结构化决策、委派状态和审批等页面可见事实。SSE 客户端应保存最后一个 sequence，重连时通过 next_sequence 或 Last-Event-ID 恢复。
