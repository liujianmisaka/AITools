# V3 独立 Composition Kernel

## 1. 定位

Composition Kernel 是一个独立、可扩展、Python 原生的运行时包。它借鉴 Cordis 的 Module、Service、Scope、Effect、Event 和 Profile 思想，但不复制 Cordis 的 TypeScript 类型系统、Node.js 运行时或动态插件生态。

Kernel 是通用基础设施，不属于 Agent、A2A、Workflow 或 Control Plane。

## 2. Kernel 的职责

Kernel 只负责：

- Module Manifest 解析和依赖验证；
- Service Definition 和 Provider Binding；
- HostContext 和显式服务访问；
- named Provider Registry；
- Capability Directory；
- LifecycleScope 和 disposer；
- EventDispatcher；
- Profile Loader；
- 启动失败回滚；
- 运行时依赖和不变式诊断。

Kernel 不负责：

- Agent 或 LLM 业务语义；
- A2A wire protocol；
- Workflow/DAG 状态机；
- Temporal 或 PostgreSQL 连接；
- Provider SDK 调用；
- UI、HTTP 路由和文件格式。

## 3. 核心角色

~~~text
Module
  -> declares requires/provides/conflicts
  -> attaches services
  -> starts runtime resources
  -> returns disposers

Service Definition
  -> stable key, version, multiplicity, contract

Provider Binding
  -> named implementation of a Service Definition

Consumer
  -> explicitly requires a service from HostContext

Profile
  -> selects modules, providers and configuration
~~~

## 4. Module Manifest

每个 Module 必须声明：

~~~text
module_id
version
requires
optional_requires
provides
conflicts
configuration_schema
~~~

启动流程：

~~~text
discover
  -> validate manifests
  -> resolve bindings
  -> topological sort
  -> attach modules
  -> start modules
~~~

任一阶段失败，必须逆序调用已完成模块的 disposer，并等待资源完全释放。

## 5. HostContext

HostContext 是 Host-scoped 的显式依赖访问入口，不是进程级全局 Service Locator。

~~~python
class HostContext:
    def require(self, service_key: str) -> object: ...
    def optional(self, service_key: str) -> object | None: ...
    def scope(self, name: str) -> LifecycleScope: ...
    def emit(self, event: RuntimeEvent) -> None: ...
~~~

测试可以创建多个互相隔离的 Host。一个进程中允许并存多个不同 Profile，但它们不能共享未声明的全局可变服务。

## 6. Service Registry

支持三种服务形态：

| 形态 | 语义 |
|---|---|
| singleton | 一个 Host 内唯一实现 |
| named provider | 多个命名实现并存，由 Profile 显式选择 |
| scoped | 绑定到 Host 或子 Scope 的实现 |

同名 Provider 不允许静默覆盖。重复、缺失、版本不兼容和冲突都必须在启动时失败。

## 7. 生命周期和 Scope

Host：

~~~text
created -> loading -> active -> draining -> stopped
~~~

Module：

~~~text
declared -> validated -> attaching -> attached -> starting -> active
active -> stopping -> disposed
~~~

Scope 要求：

- disposer 幂等；
- 停止先关闭新请求准入；
- 子 Scope 先于父 Scope 释放；
- 等待子任务、监听器和进程完全结束；
- 回调异常隔离，不阻塞其他 disposer；
- 启动失败自动回滚。

## 8. EventDispatcher

Kernel 只定义分发语义，不定义业务事件名称：

| 模式 | 用途 |
|---|---|
| emit | 已发生事实或通知 |
| waterfall | 准入、策略和请求改写 |
| serial | 顺序敏感的生命周期操作 |
| parallel | 相互独立的观察和遥测 |

Durable Event Log 是独立 Persistence Seam。EventDispatcher 不自动承担事实源职责。

## 9. Provider 装载和安全边界

可以使用 Python entry points 发现可安装 Module，但 Profile 必须显式选择是否启用。安装一个包不等于自动加载其 Provider。

第一阶段 Module 被视为受信任代码；Kernel 不提供 Python 代码沙箱。需要隔离时，使用 Process、Sandbox 或 A2A Provider。

## 10. 独立包边界

建议 Kernel 独立为：

~~~text
multi-agent-kernel/
  contracts/
  host/
  modules/
  registry/
  lifecycle/
  scopes/
  events/
  profiles/
  diagnostics/
~~~

依赖限制：仅允许标准库和稳定的基础类型依赖；不得依赖 FastAPI、Temporal、SQLAlchemy、Codex SDK、A2A 或 Workflow。

