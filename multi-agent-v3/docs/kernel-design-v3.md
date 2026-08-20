# V3 Composition Kernel

## 1. 定位

Composition Kernel 是通用的模块组合、服务绑定、作用域和生命周期内核。它不拥有 Agent、Delegation、Tool、A2A、Workflow、Execution 或 Managed Service 的业务语义。

Kernel 的设计目标不是复制 Cordis，而是吸收以下可验证原则：

- 服务通过显式上下文访问；
- 注册是可撤销的副作用；
- 作用域拥有资源和监听器；
- 依赖可声明、可验证、可诊断；
- 失败启动可以完整回滚；
- 模块可以被 Profile 组合和替换。

## 2. Kernel 的职责

Kernel 负责：

- Module Manifest；
- Profile / Composition Snapshot；
- HostContext；
- Kernel Service Binding Registry；
- Lifecycle Scope；
- Effect / Disposer；
- Event Declaration 和 EventDispatcher；
- 依赖拓扑、版本、冲突和重复绑定验证；
- 启动失败回滚；
- 模块状态和资源诊断。

Kernel 不负责：

- Capability、Provider、Coordinator 或 Profile 业务规则；
- Invocation、Execution、Activation 或 Session 状态；
- A2A wire protocol；
- Workflow/DAG 状态机；
- Temporal、PostgreSQL 或其他基础设施连接；
- 进程启动、网络策略、凭据解析或 UI 路由。

## 3. Module 和 Service Binding

```text
Module
  -> declares requires/provides/conflicts/configuration
  -> attaches services and listeners
  -> starts resources
  -> returns effect-scoped disposer

Kernel Service
  -> HostContext 中的依赖绑定

Capability Provider
  -> 通过 Kernel Service 或 Profile Port 被绑定
```

Kernel Service 不等于 Managed Service。Managed Service 由 Managed Service Runtime 管理。

## 4. Composition Snapshot

Profile 加载后生成不可变的 Composition Snapshot：

```text
profile_id
selected_modules
provider_bindings
configuration_layers
effective_configuration_hash
composition_version
```

运行中的 Host 使用一个明确的 Snapshot。未来发生重载或 Provider 切换时：

- 新模块和新 Provider 使用新 Snapshot；
- 已接受的 Execution 继续使用其 Activation 所绑定的旧 Snapshot；
- 只有旧 Snapshot 无活动 owner、Execution 和子资源时才允许释放。

当前阶段可以只实现静态 Snapshot，但不能把静态实现写成唯一架构语义。

## 5. HostContext

HostContext 是 Host-scoped 的显式依赖访问入口，不是进程级全局 Service Locator。

```python
class HostContext:
    def require(self, service_key: str) -> object: ...
    def optional(self, service_key: str) -> object | None: ...
    def scope(self, name: str) -> LifecycleScope: ...
    def emit(self, event: RuntimeEvent) -> None: ...
```

HostContext 只暴露已经声明的服务。Consumer 不得绕过 HostContext 读取未声明的全局可变对象。

## 6. Scope 和 Effect

Kernel 至少区分两种 Scope：

```text
LifecycleScope
  负责 disposer、子任务、监听器和资源释放

ExecutionScope / OwnerScope
  负责 owner、可见性、权限和资源访问
```

一个模块可以同时拥有两者，但不能用生命周期 Scope 名称替代授权身份。

每个注册都必须返回幂等 disposer：

```text
register -> disposer
dispose  -> remove registration -> await cleanup
```

Provider、Capability、Event Listener、Projection、Job Controller 和 Tool Registration 都适用这一规则。

## 7. Service Registry

Kernel Service Registry 支持：

- singleton：Host 内唯一实现；
- named：多个具名 Provider 并存，由 Profile 选择；
- scoped：按 Lifecycle/Owner Scope 创建实例。

重复、缺失、版本不匹配、绑定歧义和冲突必须在启动或重新绑定前失败。

Service Registry 不承担 Capability Catalog 职责：

```text
Service Registry
  解决依赖绑定

Capability Catalog
  解决能力发现、Provider 描述和能力协商
```

## 8. EventDispatcher

事件声明是 Contract 的一部分，必须包含：

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

分发模式：

| 模式 | 语义 |
|---|---|
| emit | 观察事件，不改变主流程 |
| parallel | 并行观察并等待全部监听器 |
| serial | 按序执行并等待 |
| bail | 第一个明确决策停止 |
| waterfall | 环绕调用，可改写请求或结果 |

监听器异常必须隔离并记录。Decision Event 的失败策略必须在 Contract 中明确，不能使用默认事件模式推断。

EventDispatcher 只负责声明的 Runtime/Decision Event 分发，不拥有 Interaction Message 的事实、序号或投递状态。Interaction Channel 必须通过独立的 Message Store/Channel Port 实现，避免把可重放消息退化为进程内监听器回调。

## 9. Profile Loader

Profile Loader 负责：

- 解析 Composition Snapshot；
- 验证模块身份和配置 Schema；
- 解析 Provider Binding；
- 拓扑排序；
- attach/start；
- 启动失败回滚；
- 停止时逆序 dispose。

配置层应支持：

```text
schema defaults
  -> profile base
  -> user layer
  -> runtime overlay
```

Secret 只能以 CredentialRef 出现，不能把机密值直接放入 Composition Snapshot。

## 10. 安全边界

Kernel 不提供 Python 代码沙箱，也不直接实现认证和授权。需要隔离时由 Sandbox、Process、Credential 和 Authorization Seam 提供。

Kernel 必须保证：

- 未声明的服务不可见；
- 已 dispose 的 Scope 不可继续注册；
- Provider 移除不会静默改变已有 Execution 的 Provider；
- Listener 和资源清理不会留下未等待的后台任务。
