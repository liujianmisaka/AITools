# V3 Capability Seams 与 Provider Contract

## 1. 定位

Seam 是稳定的能力或资源端口。它由 Definition、Provider 和 Consumer 三个角色组成，但三者可以由不同模块实现。

```text
Capability / Resource Definition
    -> Provider / Adapter
        -> Consumer / Coordinator / Profile
```

Seam 定义领域语义，不由当前实现包或某一个 Provider 决定。

## 2. Seam 分类

### Invocation Capabilities

| Seam | 语义 | 典型 Provider |
|---|---|---|
| Agent Invocation | Agent 请求、流式输出、取消、恢复 | Codex、Fake、其他 Agent Runtime |
| Delegation | Agent 委派一次性或可继续的子 Agent 工作 | In-process、ACP、CLI、A2A |
| Continuation / Interaction | 可持续调用、控制消息、回复、游标和重连 | In-process Channel、IPC、CLI、A2A、HTTP |
| Tool Execution | 工具描述、准入、执行、结果归一化 | Local Tool、MCP Tool、Remote Tool |
| Event Source | 外部事件产生、去重和确认 | Git、Webhook、Cron、内部事件 |
| Human Approval | 人工决策和审批回执 | Local UI、CLI、Remote Approval |

### Resource Capabilities

| Seam | 语义 | 典型 Provider |
|---|---|---|
| Session Log | Session Header、事件追加、恢复和读取 | JSONL、SQLite、PostgreSQL、Provider Native Bridge |
| Workspace | 工作区解析、隔离、锁和清理 | Local Git、Remote Workspace、Sandbox |
| Process | 进程启动、监督、取消和清理 | Windows Job Object、Local Process、Remote Worker |
| Artifact | 输出、日志、补丁和证据引用 | Filesystem、Object Storage、Memory |
| Persistence | Durable Fact、Execution Store、Projection Store | Memory、JSONL、PostgreSQL |
| Resource Lease | owner、epoch、过期、fencing 和释放 | Memory、PostgreSQL、Distributed Lease |

### Policy and Security Capabilities

| Seam | 语义 | 典型 Provider |
|---|---|---|
| Policy Decision | allow、deny、approval 和约束 | Static、Approval、Remote Policy |
| Sandbox | 对文件、网络、进程和工具效果的强制边界 | Local Sandbox、Container、MicroVM、Remote Sandbox |
| Credential | 引用解析、脱敏描述、轮换 | Environment、File、Vault、OS Credential Store |
| Settings | Schema、默认值、分层配置、revision 和变更 | File、Database、Memory |
| Authorization | owner、scope、资源和操作授权 | Local、LAN、Remote AuthZ |
| Decision Gate | 提案版本、效果范围和副作用前决策 | Human Approval、Policy、Remote Decision |

## 3. Provider Contract

每个 Provider 必须声明：

```text
provider_id
capability_id
version
operations
features
input_schema
output_schema
resource_requirements
cancellation
streaming
persistence
reconciliation
ownership
continuation
interaction
decision_boundary
```

请求在外部副作用发生前完成协商。不支持的能力必须显式拒绝：

```text
capability.unsupported
capability.operation_unavailable
capability.schema_rejected
capability.cancellation_unavailable
capability.persistence_unavailable
capability.policy_unavailable
capability.sandbox_unavailable
```

禁止接受请求后静默忽略字段或降级安全策略。

## 4. 生命周期和可逆注册

Provider 注册是可逆副作用：

```text
register -> publish descriptor -> accept new work
remove   -> reject new work -> keep accepted work -> dispose resources
```

注册必须返回 disposer。disposer 必须幂等，并等待 Provider 资源完全释放。

Provider 的 `start()` 必须定义发布边界：

- 发布前失败：清理所有部分资源，不产生可观察的运行记录；
- 发布后失败：通过 Run/Execution 终态表达，不把已发布资源伪装成未启动；
- Handle 丢失：只能 attach 或 reconcile，不能盲目重复启动；
- dispose：等待工作、监听器、进程和子资源完全停稳。

## 5. Delegation Contract

本文的 Continuation、Interaction Message 和 Decision Gate 语义以 [Delegation、Continuation 与 Interaction Contract](delegation-continuation-v3.md) 为准。

Delegation 不等同于 A2A。Delegation Provider 可以是进程内、ACP、CLI、Codex、Claude Code 或 A2A。

Delegation Contract 必须区分：

### One-shot

- 一个请求；
- 一个结果；
- 一个可 dispose 的 Run；
- 失败通过结构化 stop reason 返回。

### Continuable

- 一个持久 Session；
- 至多一个 live Activation；
- 多个 FIFO follow-up；
- 有序的 control/message channel；
- accepted、delivered、processed、completed 四类投递事实；
- correlation、reply_to、cursor 和 ack；
- question、reply、steer、pause、resume 和 cancel；
- parent/child owner；
- 冷恢复和 Activation Reconciliation；
- child report 和生命周期通知。

Provider 只负责其传输和创建能力。Continuable Activation 的身份、所有权、消息事实、权限、队列和释放由调用方的 Delegation/Continuation 实现负责。

`parent/child` 是一种通用 owner relationship，不表示特定 Agent、A2A 或 Codex 层次。

## 6. Tool Execution Pipeline

Tool Provider 不应独自决定完整执行流程。统一流水线为：

```text
tool/call
  -> preflight
  -> approval
  -> monotonic guards
  -> sandbox resolution
  -> execute wrapper
  -> provider execute
  -> post-execute
  -> output normalization
  -> finalize
  -> tool/result
```

其中：

- preflight 可以拒绝请求；
- approval 只能授予当前操作需要的权限；
- monotonic guard 不能被后续 Provider 覆盖；
- sandbox 必须 fail closed；
- output normalization 统一异常、超时、取消和 Schema 错误；
- finalize 之后的结果不可被监听器改写。

## 7. Session Log Contract

Session Log 至少包含：

- immutable Session Header；
- format version；
- parent/seed lineage；
- workspace/profile/composition identity；
- append-only events；
- source revision；
- replay/inspect/load 区分；
- projection watermark。

如果 Session 支持持续交互，还必须能关联：

- Interaction Channel；
- message sequence 和 cursor；
- provider session reference；
- current/live Activation；
- continuation request 和 reply；
- decision/proposal revision。

模型可见内容必须可以从 Session Log 重建。Provider Native Session 只能作为一个外部资源引用。

## 8. Credential 和 Settings Contract

Credential：

- 配置只保存 CredentialRef；
- `resolve()` 每次操作重新读取；
- `describe()` 永不返回 Secret；
- 空值视为未配置；
- Secret 不进入 Event、Artifact、日志或 UI 响应。

Settings：

- Schema、默认值、Profile Base、User Layer 分离；
- `update` 使用稀疏 patch；
- `replace` 明确表示删除用户覆盖并重新继承；
- 写入携带 expected revision；
- live/restart 生效时机必须显式声明。

## 9. Event Contract

每个事件必须声明：

```text
event_name
version
mode
payload_schema
scope
producer
consumer
failure_isolation
```

模式至少包括：

```text
emit       观察，不改变主流程
parallel   并行观察并等待
serial     按序执行并等待
bail       第一个明确决策停止
waterfall  可改写请求或结果的环绕链
```

事件必须按 Scope 路由，不能让不同 Agent 默认共享所有内部事件。

### Interaction Message Contract

Interaction Message 是可寻址、可重放的控制或信息事实，不等同于 Runtime Event。至少包含：

```text
message_id
channel_id
sender_principal
recipient_principal
message_type
payload_schema
correlation_id
causation_id
reply_to
sequence
delivery_status
scope
```

Transport 可以丢失实时推送，但不能丢失已经接受的 Message Fact。
