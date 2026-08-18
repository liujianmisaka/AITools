# V3 Capability Seams

## 1. Seam 分类

| Seam | Definition | Provider 示例 | Consumer 示例 |
|---|---|---|---|
| Agent Invocation | Agent 调用、流式输出、取消和恢复 | Codex、Fake、Claude、A2A | Direct Coordinator、A2A TaskHandler |
| Tool Execution | 工具发现、准入和执行 | Local Tool、MCP Tool | Agent Runtime、Coordinator |
| Workspace | 工作区解析、隔离、锁和清理 | Local Git、Sandbox Workspace | Agent Provider、Tool Provider |
| Process | 子进程启动、监督、取消和清理 | Windows Local、Job Object | Codex Provider、Shell Tool |
| Policy | 权限、网络、工具和数据访问决策 | Local Policy、Approval Policy | Invocation Runtime、Tool Runtime |
| Artifact | 输出、日志、补丁和证据 | Filesystem、S3-like、Memory | Invocation Runtime、A2A Server |
| Session | 会话创建、恢复、追加和查询 | JSONL、PostgreSQL、Provider Native | Agent Provider、A2A Client |
| Event Source | Webhook、Git、Cron、内部事件 | CloudEvent、Git Poller、Timer | Reactive Coordinator |
| Human Approval | 人工准入和回答 | Local UI、CLI | Policy、Tool Runtime |
| A2A Transport | Agent Card、Task、Message、Stream | HTTP/SSE、Fake | A2A Client/Server |
| Persistence | Durable Store 和 Event Log | Memory、JSONL、PostgreSQL | Runtime、Coordinator、A2A |

## 2. Provider 能力协商

Provider 在启动前发布静态描述：

~~~text
CapabilityDescriptor
  id
  version
  operations
  features
  input_schema
  output_schema
  cancellation
  streaming
  persistence
  resource_requirements
~~~

请求如果需要 Provider 不支持的能力，必须返回稳定错误：

~~~text
capability.unsupported
capability.schema_rejected
capability.cancellation_unavailable
capability.persistence_unavailable
capability.policy_unavailable
~~~

禁止“接受请求后忽略字段”的静默降级。

## 3. 生命周期要求

每个 Provider 必须定义：

- prepare 是否产生外部副作用；
- start 的持久化边界；
- cancel 是否等待实际停止；
- dispose 是否等待资源完全释放；
- 崩溃后如何 reconcile；
- 是否允许同一 Session 并行 Activation。

## 4. 包拆分规则

当 Definition、Provider、Consumer 需要独立演进时拆成多个包；单一用途且没有独立替换价值的实现保持一个包。

包名描述稳定职责，不使用首个实现或未来扩展命名。例如：

~~~text
agent-invocation        # Definition
agent-provider-codex    # Provider
a2a-task-handler        # Consumer/Adapter
persistence-postgres    # Backend
coordinator-temporal    # Coordinator
~~~

