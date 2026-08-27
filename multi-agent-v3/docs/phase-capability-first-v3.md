# V3 领域优先实施阶段

V3 允许破坏性重构，不做 V2 兼容迁移。实施顺序先建立领域 Contract 和不变量，再调整当前实现。当前包目录不约束目标架构。

Continuation、Interaction Message、Delegation 和 Decision Gate 的通用门禁见 [Delegation、Continuation 与 Interaction Contract](delegation-continuation-v3.md)。

## M0：领域模型和架构基线

定义并冻结：

- Capability；
- Principal / Scope；
- Invocation Intent；
- Execution；
- Activation；
- Session；
- Delegation；
- Continuation / Interaction Channel；
- Decision Gate；
- Resource Lease；
- Managed Service；
- Task / Job Projection；
- Durable Fact、Runtime Event、Decision Event、Projection。

门禁：每个对象都有唯一事实所有者、生命周期、owner、终态和失败语义。

## M1：Contract Plane

实现与实现无关的 Contract：

- Domain Contracts；
- Capability / Resource Seams；
- Execution / Activation / Reconcile；
- Principal / Scope / Delegation；
- Continuation / Interaction Message / Cursor；
- Proposal / Decision Gate；
- Session Log；
- Persistence Port；
- Resource Lease；
- Sandbox；
- Credential；
- Settings；
- Managed Service；
- Event Declaration。

门禁：Contract Plane 不依赖数据库、Temporal、Provider SDK、操作系统 API 或 UI。

## M2：Composition Kernel

实现：

- Module Manifest；
- Composition Snapshot；
- HostContext；
- Service Binding Registry；
- Lifecycle Scope；
- Owner Scope；
- Effect / Disposer；
- Typed Event Dispatcher；
- Profile Loader；
- 启动失败回滚。

门禁：模块、Provider、事件和 Projection 注册都可撤销；Host 停止后无未等待资源。

## M3：Execution Runtime

实现：

- Invocation Intent 接纳；
- Execution 状态机；
- Activation 发布边界；
- owner、lease、epoch、fencing；
- Capability negotiation；
- 幂等；
- 取消和停止确认；
- Provider 错误归一化；
- Reconciliation；
- Completion Boundary。

Execution Runtime 只负责单次 Invocation。Delegation 和 Continuation 通过已有 Seam/Coordinator 组合，不把父子交互写死到 Invocation 状态机。

门禁：Fake、Codex、A2A 通过同一组参数化 `InvocationProvider` Contract Test；各 Provider 的
Continuation、远程恢复等扩展契约继续单独验证；崩溃、取消、重复提交和不确定状态不会产生第二个
live write Activation。

## M4：Session、Persistence 和 Projection

实现：

- Session Header；
- append-only Session Log；
- Interaction Channel、Message Fact 和 cursor；
- Execution Fact Store；
- Projection Store；
- revision / watermark；
- load / inspect / replay；
- format version 和 unsupported/corruption 错误；
- 冷恢复和中断轮次处理。

门禁：Projection 可以从 Durable Fact 重建；Provider Native Session 不成为唯一平台事实源。

## M5：基础 Capability Seams

实现：

- Agent Invocation；
- Tool Execution Pipeline；
- Workspace；
- Process；
- Artifact；
- Policy；
- Sandbox；
- Credential；
- Settings；
- Resource Lease；
- Human Approval。

需要验证 Decision Gate 在副作用前绑定 proposal revision、effect scope 和 decision principal。

门禁：能力不足 fail closed；Provider 注册和移除可逆；安全策略不能被 Provider 静默忽略。

## M6：Delegation 和 A2A

实现：

- Delegation Contract；
- one-shot Run；
- continuable Session / Activation；
- parent/child owner；
- delegation depth；
- tool filter / persona；
- child identity / projection；
- report back；
- follow-up、reply、question、steer、pause、resume 和 ack；
- message accepted/delivered/processed/completed；
- A2A Provider；
- A2A HTTP/SSE Transport。

门禁：A2A 只是 Delegation 的一种 Provider/Transport；Delegation、Interaction Message、A2A Task、Execution、Activation 和 Session 身份分别可追踪，且 Continuation 不会制造第二个 live write Activation。

## M7：Coordinator

实现：

- Direct；
- Reactive；
- Queue；
- Event Source；
- Retry；
- Cancel；
- Durable Coordinator。

门禁：Coordinator 只组合 Execution、Delegation 或 Interaction Port，不直接操作 Provider SDK、数据库内部 API 或工作区。

## M8：Managed Service Runtime

实现：

- Service Definition；
- Service Catalog；
- Process Supervisor；
- Health Probe；
- PID/epoch；
- graceful shutdown；
- process tree cleanup；
- service failure and recovery policy。

门禁：Managed Service 生命周期不推进 Execution、Task 或 Session 终态。

## M9：Application Profiles 和 Transport

组合：

- standalone-agent；
- a2a-node；
- agent-host；
- a2a-agent-host；
- local-delegation；
- durable-agent；
- control-plane；
- service-host；
- control-plane-workflow；
- FastAPI、A2A HTTP、CLI 和 React。

门禁：Profile 显式声明所有 Provider、事实源、Projection、owner 和配置；Transport/UI 不直接进入底层实现。

## M10：当前实现迁移

把当前代码映射到目标 Contract 和 Runtime：

- 先迁移事实所有者和生命周期；
- 再迁移 Provider 注册和 disposer；
- 再迁移 Session/Persistence；
- 再迁移 Delegation/A2A；
- 最后迁移 Coordinator、Profile 和 UI。

迁移映射只记录当前代码事实，不修改目标架构语义。

## 总验收

1. Contract Plane 可独立构建和测试；
2. Kernel 可以组合、重载和完整停止；
3. Provider 注册可逆且已有 Execution 不受错误移除影响；
4. Execution/Activation 的 owner、lease、epoch 和 Reconciliation 测试通过；
5. Session Log 和 Projection 可重建；
6. Sandbox、Credential 和 Settings 的边界通过；
7. Delegation 与 A2A 的身份和所有权可追踪；
8. Continuation 的消息事实、游标和恢复语义可重建；
9. Decision Gate 能阻断未确认的副作用；
10. Coordinator 不依赖具体 Provider；
11. Managed Service 不与 Execution 状态混淆；
12. 所有 Profile 启停后无残留进程、端口、任务和后台资源。
