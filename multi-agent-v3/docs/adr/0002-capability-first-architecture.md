# ADR-0002：以领域 Contract、Capability 和 Composition Kernel 为核心

- 状态：Proposed
- 日期：2026-08-19
- 适用范围：V3 目标架构
- 取代：V2 中 Workflow/Temporal 作为平台核心的决策

## 背景

平台需要支持本地 Agent、远程 Agent、A2A Delegation、Tool、Workspace、Event Source、Cron、Git 触发和长期服务。若以某一种 Workflow、Provider 或 Transport 作为核心，新增能力就会被迫进入已有实现的生命周期和数据模型。

当前实现中的 package、Job、Task 和 Service 结构只能说明已经发生的耦合，不能作为目标架构的来源。

## 决策

1. 架构由领域对象、不变量和生命周期推导，不由当前目录反向推导。
2. Capability 是稳定的可发现能力；Provider 是其具体实现；Consumer 通过 Contract 或 Host Context 使用能力。
3. Principal/Scope、Invocation Intent、Execution、Activation、Session、Delegation、Continuation/Interaction Channel、Decision Gate、Resource Lease 和 Managed Service 分别建模。
4. 一个 Execution 可以拥有多个历史 Activation，但同一时间最多一个 live Activation。
5. Composition Kernel 只负责 Module、Context、Binding、Scope、Effect、Event 和 Composition Snapshot。
6. Execution Runtime 管理 Invocation、Execution、Activation、Lease、取消、幂等和 Reconciliation。
7. Managed Service Runtime 管理长期运行的服务进程，不推进 Execution 或 Task 终态。
8. Delegation 是独立 Capability；A2A 是一种 Delegation Provider 和 Transport。
9. Continuation 和 Interaction Message 是通用 Contract；消息事实、游标、投递状态和回复关系不能由某个 Transport 私有拥有。
10. Decision Gate 在副作用前绑定提案版本、效果范围和决策主体。
11. Persistence 是 Port + Adapter；Projection 不得成为事实源。
12. Session Native ID 不替代平台 Session Log。
13. Sandbox、Credential、Settings、Authorization 和 Observability 是横向 Port。
14. Provider 注册、事件监听、Projection 和 Tool Registration 都必须是可撤销的生命周期副作用。
15. Workflow、DAG、State Machine、Cron 和 Temporal 都是可选 Coordinator 或 Service Adapter。
16. Task/Job 是协议包装或应用投影，不能在没有明确事实所有者的情况下独立推进另一套终态。
17. V3 不保留 V2 API、数据库模型和双写兼容层。

## 不变量

- 外部副作用已经开始但终态无法证明时，必须进入 `reconciliation_required`；
- 取消只有在 Provider 确认停止后才能成为 `cancelled`；
- 旧 owner 或旧 epoch 不能修改新状态；
- Principal 必须通过 Scope 获得控制权，知道资源 ID 不等于拥有资源；
- Provider 不支持请求能力时必须在外部副作用前拒绝；
- Continuation 不得在同一 Session 创建两个 live write Activation；
- Message 的 accepted、delivered、processed 和 completed 必须可区分；
- Durable Fact 可以重建 Projection；
- Secret 不得出现在 Fact、Event、Artifact、日志或 UI 响应；
- dispose 必须等待所有子资源完全停稳。

## 明确不采用

- 不把当前 Python 包目录当作目标分层；
- 不把 A2A 当作全部 Agent-to-Agent 语义；
- 不为每一种当前状态类建立新的架构层；
- 不因某个 Agent、Provider 或近期使用场景新增固定架构层；
- 不让 Kernel 直接知道 Agent、A2A、Workflow、Provider SDK 或数据库；
- 不让 Coordinator 直接导入 Provider SDK；
- 不让 Control API 直接启动 subprocess；
- 不把 Projection、UI 或 Transport 当作事实源；
- 不以静默降级换取 Provider 兼容；
- 不在规范架构文档中记录当前实现状态。

## 后果

正面结果：

- Provider、Coordinator 和 Transport 可以独立替换；
- A2A、in-process Delegation 和 CLI Delegation 可以共享同一 Delegation Contract；
- Execution、Session 和 Managed Service 的生命周期不会互相污染；
- 当前实现可以破坏性迁移，不需要保留兼容包装；
- 持久事实、实时事件和 Projection 的责任明确。

代价：

- 需要重新定义当前 Job、Task、Session 和 Provider API；
- 需要建立 Session Log、Lease、Sandbox、Credential 和 Settings Contract；
- 需要增加 Provider disposer、owner fencing、Projection 重建和崩溃窗口测试；
- 迁移期间必须维护独立的实现映射，但不能让映射反向修改目标架构。
