# V3 当前实现迁移映射

> 性质：非规范、可变迁移文档
> 作用：记录当前代码到领域优先目标架构的迁移方向
> 约束：本文不得反向修改 [architecture-capability-first-v3.md](architecture-capability-first-v3.md) 的领域语义

## 1. 使用方式

目标架构先于当前实现。当前实现映射只回答：

- 当前代码承载了哪些目标语义；
- 哪些语义被多个实现重复拥有；
- 哪些目标 Contract 尚未落地；
- 迁移时需要保留哪些行为；
- 哪些当前接口应被删除或重构。

它不回答目标架构应该有哪些层。

## 2. 当前实现的迁移观察

| 当前实现观察 | 目标语义 | 迁移方向 |
|---|---|---|
| Provider 通过 Module attach 注册到 Execution Runtime | 可逆 Provider Binding | 增加 registration disposer、Provider epoch 和 remove 语义 |
| Provider Registry 位于 Execution Runtime 内部 | Capability Catalog + Execution Provider Binding | 分离能力发现和执行生命周期 |
| Session Store 主要承担 claim/release | Session Log + Session Ownership | 增加 Header、Event Log、Revision、Projection、Continuation 和恢复语义 |
| `InvocationRequest.parent_invocation_id` 记录请求来源 | Delegation parent/child relationship | 增加 Delegation Fact、Principal/Scope、预算、报告和权限校验；不能把该字段当作完整委派模型 |
| InvocationRuntime 维护进程内 active handles/tasks | Execution Fact + Activation Reconciliation | 已将 owner、scope、lease owner/epoch 和 resource refs 纳入 Invocation Contract/Memory/JSONL Fact，并对后续事实做 fencing；仍需继续补齐可持久化 Activation、Provider Session Reference、恢复 worker 和未知外部副作用对账 |
| InvocationEvent 只按 invocation_id 排序 | Interaction Channel + Message Fact | 增加 channel、message、cursor、correlation、delivery status 和 reply 语义 |
| Codex Provider 以 `_session_owners` 内存字典串行 Native Session | Provider Session Lease | 将 claim/epoch/ownership 移到通用 Session/Lease Port；Provider 内存表只能作为本地优化 |
| Codex Provider `thread_start/thread_resume` 直接启动单轮 turn | Continuation Contract | 拆分 prepare/start/follow-up/attach/reconcile，明确 Provider 能力不足时的拒绝和对账 |
| A2A Task Store 保存协议状态 | Task Projection / Protocol Fact | 明确与 Execution Fact Store 的关系 |
| A2A TaskRequest 的 context/message 字段映射一次 Invocation | A2A Delegation Provider | 增加 continuable follow-up、reply、cursor、owner/scope 和 report 映射，不让 Task ID 取代 Delegation/Session |
| Control Plane DurableJob 保存应用状态 | Job Projection / Application Record | 禁止与 Execution Fact 无说明地双向推进 |
| Control Plane 只有 jobs/templates/instances/approvals 入口 | Delegation Gateway Port | 增加通用 delegation create/send/events/reply/cancel/reconcile 适配；Transport 不直达 Provider |
| Queue Coordinator 自己维护 Job 状态 | Coordinator-local projection | 复用 Execution Port，删除重复终态事实 |
| Codex Provider 读取 request.input 中的 sandbox | Sandbox Policy Port | 将安全策略在 Runtime 边界统一解析并 fail closed |
| ProfileDefinition 使用静态 module/config 列表 | Composition Snapshot | 增加配置层、绑定 epoch 和未来 rebind 语义 |
| LifecycleScope 负责资源释放 | Lifecycle Scope | 与 Owner/Execution Scope 分离 |
| RuntimeEvent 为字符串和 JSON payload | Typed Event Contract | 已增加 EventDeclaration、版本、模式、payload Schema、producer/consumer、scope、failure isolation 和可撤销 Dispatcher 绑定 |
| Tool Provider 直接执行 handler | Tool Execution Pipeline | 增加 preflight、approval、sandbox、normalize 和 finalize |
| Control Plane 通过 ServiceManager 管理服务 | Managed Service Runtime | 保留独立服务生命周期，不混入 Execution 事实 |
| Approval 目前绑定模板实例或 Job 投影 | Decision Gate | 抽象 proposal revision、plan hash、effect scope 和 decision principal，作为副作用前通用准入 |

## 3. 迁移顺序

```text
Domain Contracts
  -> Execution / Activation / Lease
    -> Session Log / Interaction Channel / Persistence
      -> Provider registration and disposal
        -> Delegation / Continuation / A2A
          -> Coordinator / Gateway
            -> Profile
              -> Transport / UI
```

每一步完成后，旧实现可以被删除；不建立 V2/V3 双写或长期兼容层。

## 4. 当前代码不能成为目标语义的依据

以下当前实现特征不应直接提升为架构概念：

- `QueueJob`、`DurableJob` 和 A2A `Task` 的现有字段；
- `parent_invocation_id` 是否存在；
- 某个 Provider 是否恰好支持 follow-up 或 resume；
- Provider 当前是否通过 `HostContext` 注册；
- 当前文件夹名称和 Python 包名；
- 当前 Memory Store 的状态转换；
- 当前 Control Plane 的 API 路由；
- 当前某一个 Provider 的 sandbox、model 或 effort 参数；
- 当前 Web UI 的页面结构。

这些内容只能作为迁移输入和行为验收样本。

## 5. 迁移完成标准

迁移完成的判断依据是目标 Contract 和不变量，而不是当前包是否被保留：

- 每个目标对象只有一个事实所有者；
- 每个注册都有 disposer；
- 每个 live Activation 都有 owner 和 lease；
- 每个未知外部副作用都进入 reconciliation；
- 每个 Projection 可以从 Durable Fact 重建；
- 每个 Continuation 的 Message Fact 可以按 cursor 重建；
- 每个 Delegation 的 owner、scope、Decision Gate 和 child report 可审计；
- Provider、Coordinator、Profile 和 Transport 的依赖方向符合目标矩阵；
- 删除旧实现后，目标 Profile 的 Contract Test 和真实入口测试仍然通过。
