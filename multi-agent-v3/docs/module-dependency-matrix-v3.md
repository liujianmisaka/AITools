# V3 目标模块依赖矩阵

> 本文是目标架构规范，不描述当前目录和当前实现状态。当前实现迁移关系见[实现迁移映射](implementation-map-v3.md)。

## 1. 目标依赖方向

```text
Transport / UI
  -> Application Profile / Composition Host
      -> Coordinator
      -> Delegation / Continuation Controller
      -> Provider / Infrastructure Adapter
      -> Execution Runtime
      -> Managed Service Runtime
          -> Composition Kernel
              -> Contract Plane

Capability / Resource Seam
  -> Contract Plane

Provider / Adapter
  -> Seam Contract
  -> Execution or Service Runtime
  -> Kernel Module API
```

依赖箭头表示编译期和组合期依赖。运行时消息流可以通过 Event、Queue、RPC 或 Stream 反向传递，但不能引入反向静态依赖。

## 2. 目标模块类别

| 模块类别 | 可依赖 | 禁止依赖 |
|---|---|---|
| Contract Plane | 标准库、稳定基础类型 | FastAPI、Temporal、数据库驱动、Provider SDK、操作系统 API、UI、Profile |
| Composition Kernel | Kernel Contracts、标准库 | 业务 Capability、Execution、Coordinator、Provider SDK、数据库、UI |
| Execution Runtime | Domain/Invocation Contracts、Kernel、Capability Ports、Persistence Ports、Policy Ports | Coordinator、Profile、Transport、具体 Provider SDK |
| Delegation / Continuation Controller | Delegation、Session、Interaction、Execution Ports、Policy、Persistence | Provider SDK、Transport 私有状态、数据库内部 API |
| Managed Service Runtime | Service Contracts、Kernel、Process/Health Ports | Invocation、Coordinator、A2A Profile、Provider SDK、UI |
| Capability / Resource Seam | Contract Plane | 具体 Provider、Profile、UI、数据库实现 |
| Provider / Adapter | Seam Contracts、对应 Runtime、Kernel Module API、基础设施 SDK | Coordinator、Profile、UI、另一 Provider 的私有实现 |
| Coordinator | Execution Runtime、Event Source Port、Persistence Port、Domain Contracts | Provider SDK、A2A Server、数据库内部 API、Workspace 私有实现、UI |
| Application Profile | Kernel、Runtime、Seam、Provider、Coordinator、Persistence、Transport | 被底层模块反向依赖 |
| Transport | Transport Contracts、Profile API、Handler Port | Provider SDK、数据库内部 API、subprocess、任意宿主私有状态 |
| UI | Profile API / BFF | subprocess、Provider SDK、任意 command/cwd/env、数据库连接 |

## 3. 领域模块边界

### Execution Runtime

Execution Runtime 是 Invocation Intent、Execution、Activation、Lease 和 Reconciliation 的唯一生命周期所有者。

Provider 不能自己推进平台 Execution 终态；Coordinator 不能直接操作 Provider Handle；Transport 不能直接取消进程。

### Managed Service Runtime

Managed Service Runtime 只管理长期服务进程。它与 Execution Runtime 共享 Process、Health 和 Lifecycle Contract，但不共享 Invocation 状态。

### Optional Background Work

如果未来出现不属于 Invocation 的后台工作，应先定义 Background Work Seam。只有当它拥有独立的 owner、output cursor、done 和 cleanup 语义时，才升级为独立 Runtime。

## 4. 关键 Port

目标架构至少需要以下 Port：

```text
CapabilityCatalog
InvocationStore / ExecutionFactStore
SessionLog
InteractionChannel / MessageStore
ProjectionStore
ResourceLease
DecisionGate
SandboxProvider
CredentialProvider
SettingsProvider
DelegationProvider
ToolExecutionPipeline
ManagedServiceSupervisor
InvariantRegistry
```

这些 Port 不绑定 JSONL、PostgreSQL、Temporal、A2A、Codex、Windows 或 React。

Delegation / Continuation Controller 可以组合这些 Port，但不能把某个 Transport 的 Task/Job 状态提升为平台事实。

## 5. Provider 注册规则

Provider 注册必须是生命周期副作用：

```text
register(provider) -> disposer
remove(provider)   -> reject new work, preserve accepted work, await cleanup
```

Provider Registry 必须支持：

- 命名 Provider；
- Capability Descriptor；
- 版本和 Feature；
- owner/scope；
- Provider epoch；
- provider-added/provider-removed 观察事件；
- 已接受 Execution 的旧 Provider 绑定。

## 6. 事件依赖规则

Event Contract 必须包含事件模式和作用域。

```text
emit       观察
parallel   并行观察
serial     顺序处理
bail       首个决策终止
waterfall  请求/结果改写
```

Durable Fact、Runtime Event、Decision Event 和 Projection Update 不得共享一个无类型状态推进方法。

## 7. Persistence 依赖规则

Persistence Adapter 只实现公开 Port：

- 不访问 Temporal 内部表；
- 不把 Projection 当作事实源；
- 不在 Transport 中直接连接数据库；
- 不将 Provider Native Session 直接当作平台 Session Log；
- 必须提供版本、revision、watermark 和 corruption/unsupported 错误语义。

## 8. 机械检查

CI 必须检查：

- 禁止循环依赖；
- Contract Plane 无基础设施依赖；
- Kernel 无业务依赖；
- Runtime 不导入 Coordinator、Profile 或 UI；
- Provider 不导入 Coordinator、Profile 或 UI；
- Coordinator 不导入 Provider SDK；
- Service Runtime 不导入 Invocation 或 A2A Profile；
- Continuation Controller 不导入具体 Provider SDK；
- Transport/UI 不导入 subprocess 或数据库；
- 每个 Provider 都有对应 Contract Test；
- 每个 Registration 都有 disposer；
- 每个 Profile 都有启动、停止、重启和清理测试；
- 每个 Projection 都有从 Durable Fact 重建的测试。
- 每个 Interaction Channel 都有消息幂等、cursor 重放和投递状态测试；
- 每个 Decision Gate 都有 proposal revision 和副作用前阻断测试。
