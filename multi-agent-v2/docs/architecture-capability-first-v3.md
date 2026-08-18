# Multi-Agent V3：Capability-First 架构

> 状态：Proposed / 下一阶段重构基线
> 日期：2026-08-18
> 兼容性：不保留 V2 API、数据模型和运行时兼容层

## 1. 目标

V3 不再把 Workflow、DAG 或 Temporal 作为项目核心。项目核心是可独立发现、调用、取消、观察和替换的能力模块。

Workflow、状态机、DAG、Cron、A2A、CLI 和 Web 都是能力组合方式或应用 Profile。它们不得成为 Agent Runtime、工具、策略、工作区或 Artifact 的所有者。

核心执行关系：

~~~text
Capability Contract
    -> Capability Provider
        -> Invocation
            -> Activation
                -> Events / Session / Artifact / Resource
~~~

相关文档：

- capability-seams-v3.md：能力 Definition、Provider、Consumer 和能力协商；
- invocation-lifecycle-v3.md：Invocation、Activation、Session 和取消边界；
- a2a-standalone-v3.md：不依赖 Control Plane 的 A2A Profile；
- kernel-design-v3.md：独立 Composition Kernel 的职责、接口和生命周期；
- module-dependency-matrix-v3.md：模块允许和禁止依赖；
- profile-catalog-v3.md：运行时组合目录；
- phase-capability-first-v3.md：破坏性重构的实施阶段。

## 2. 设计原则

1. **能力优先**：新增产品功能首先定义能力 Seam，而不是新增 Workflow 节点。
2. **Definition / Provider / Consumer 分离**：接口、实现和消费方可以独立演进。
3. **独立内核**：实现一个 Python 原生、可独立发布的 Composition Kernel；内核只负责模块组合、服务注册、生命周期、作用域和事件，不拥有 Agent、A2A 或 Workflow 业务语义。
4. **显式组合**：Profile 声明模块和绑定关系，禁止通过隐式全局单例改变运行时。
5. **能力不足显式失败**：Provider 不支持请求能力时，在启动前拒绝，不允许静默降级。
6. **持久事实与实时事件分离**：恢复和审计依赖 Durable Facts，UI 和进度依赖 Runtime Events。
7. **单一事实源**：同一 Invocation 在一个 Profile 中只能有一个状态事实源。
8. **生命周期完整**：停止必须达到完全停稳，不能只发送取消信号就返回。
9. **破坏性重构优先**：V3 直接采用新术语和新 API，不为 V2 保留兼容包装。

## 3. 分层

~~~mermaid
flowchart TB
    P["Application Profiles<br/>A2A Node / Agent Host / Control Plane / CLI"]
    C["Optional Coordinators<br/>Direct / Reactive / DAG / State Machine / Temporal"]
    S["Capability Seams<br/>Agent / Tool / Policy / Workspace / A2A / Artifact"]
    H["Composition Kernel<br/>Modules / Registry / Scope / Lifecycle / Events"]
    R["Invocation Runtime<br/>Lifecycle / Events / Resources / Idempotency"]
    K["Contracts<br/>Capability / Invocation / Session / Event / Artifact"]

    P --> C
    P --> H
    C --> H
    S --> H
    R --> H
    R --> K
~~~

### Layer 0：Contracts

只包含 Pydantic 模型、Python Protocol、错误码和 JSON Schema。不得依赖 FastAPI、Temporal、SQLAlchemy、Provider SDK、React 或 Windows 实现。

### Layer 1：Composition Kernel

Composition Kernel 是独立的 Python 包，不依赖任何业务能力。它提供 Module Manifest、HostContext、ServiceRegistry、LifecycleScope、EventDispatcher、Profile Loader 和依赖验证。CapabilityDirectory 属于 Invocation Runtime 或能力目录模块，作为普通 Kernel Module 装载。

Kernel 不知道 Agent、A2A、Workflow、Codex、Temporal 或 PostgreSQL。它不负责持久化业务状态，也不自动加载所有安装的 Provider。

### Layer 2：Invocation Runtime

负责 Invocation 的准入、生命周期、取消、事件规范化、资源所有权、幂等和不变式。它依赖 Composition Kernel，但不被 Kernel 反向依赖；它不负责选择 DAG、读取 Workflow DSL 或直接启动 Provider SDK。

### Layer 3：Capability Seams

提供 Agent、Tool、Workspace、Process、Policy、Sandbox、Artifact、Session、Event Source、Human Approval、Credentials 和 A2A 等能力定义。

### Layer 4：Coordinators

可选的调用组合器：Direct、Reactive、Queue、DAG、State Machine 和 Temporal。它们只创建和等待 Invocation，不执行 Provider 细节。

### Layer 5：Application Profiles

通过 Profile 将能力、Provider、Persistence、Transport 和 Coordinator 组合成可启动产品。

## 4. 核心术语

| 术语 | 定义 | 事实所有者 |
|---|---|---|
| Capability | 可发现的能力定义和操作集合 | Capability Directory |
| Provider | Capability 的具体实现 | Provider Runtime |
| Consumer | 调用 Capability 的模块 | Consumer |
| Invocation | 一次调用意图 | Invocation Runtime |
| Activation | Invocation 对应的真实运行实例 | Provider + Runtime |
| Session | 跨 Invocation 复用的上下文 | Session Provider |
| Artifact | 输出、日志、文件或证据引用 | Artifact Store |
| Coordinator | 多次 Invocation 的组合器 | Coordinator |
| Profile | 一组模块和绑定的运行配置 | Composition Layer |
| Module | 声明依赖、提供服务和生命周期挂载逻辑的可装载单元 | Composition Kernel |
| Scope | 具有独立资源归属和释放顺序的 Host/Module 子上下文 | Composition Kernel |
| HostContext | 当前 Profile 对模块暴露的显式服务访问和事件接口 | Composition Kernel |
| Task | A2A 或应用层对 Invocation 的外部包装 | A2A/Application |

Task 不进入底层核心数据模型；底层统一使用 Invocation。

## 5. Capability Seam 约定

每个 Seam 必须明确三类角色：

~~~text
Capability Definition
    ├── Provider
    └── Consumer
~~~

新增能力时必须同时提供：

- 稳定的 Capability ID 和版本；
- 操作名称及输入输出 Schema；
- 支持的能力特性；
- 生命周期和取消语义；
- 资源所有权；
- Durable/Runtime 事件；
- Contract Test；
- 不支持场景和失败码。

## 6. 状态事实源

不同 Profile 使用不同的唯一事实源：

| Profile | Invocation 事实源 |
|---|---|
| A2A + Memory | Memory Task/Invocation Store |
| A2A + JSONL | JSONL Durable Event Log |
| A2A + PostgreSQL | PostgreSQL Invocation Store |
| A2A + Temporal | Temporal History |
| Control Plane + Temporal | Temporal History；PostgreSQL 只作配置和投影 |
| Agent Session | Provider Session Log |

Provider Session 不等同于平台 Invocation。A2A Event Stream 也不承担终态事实。

## 7. 安全和边界

第一阶段继续保持本机/可信 LAN Profile，但所有接口必须预留：

- Authenticator；
- Authorizer；
- CapabilityPolicy；
- ReplayGuard；
- PayloadLimits；
- ArtifactAccessPolicy。

未配置认证的 Profile 只能显式绑定 loopback 或可信 LAN 监听地址，不得默认暴露互联网。

## 8. 破坏性重构原则

V3 不做以下兼容工作：

- 不保留 V2 API 路由；
- 不保留 workflow_instance、node_instance 作为核心术语；
- 不把旧 Workflow DSL 转换成新 API；
- 不双写 V2 和 V3 数据库；
- 不让新包反向导入旧 control_plane；
- 不在旧 workflow_runtime 上叠加 A2A。

旧 V2 代码只作为行为参考，V3 以新的 Contract Test 和真实入口测试为准。
