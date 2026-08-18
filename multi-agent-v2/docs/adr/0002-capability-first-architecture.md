# ADR-0002：以 Capability 和独立 Composition Kernel 为核心的模块化架构

- 状态：Proposed
- 日期：2026-08-18
- 取代：ADR-0001 中关于 Workflow/Temporal 是 V2 核心、以及不支持 A2A 的决策

## 背景

当前 V2 虽然按目录划分了 agent_runtime、workflow_runtime、eventing、policy 和 persistence，但实际发布和依赖仍然是单体结构。Workflow、Temporal 和 Control Plane 对其他能力形成了过强的反向约束。

下一阶段重点是让 Agent、Tool、A2A、Policy、Workspace、Artifact 和 Event 能力可以独立使用，再由不同 Profile 组合。开发阶段允许破坏现有功能，不需要保留兼容层。

## 决策

1. Capability 是平台的基本单位。
2. Invocation 是所有能力调用的统一边界。
3. 实现独立的 Python Composition Kernel，作为模块、服务、生命周期、作用域、事件和 Profile 的通用宿主。
4. Composition Kernel 不拥有 Agent、A2A、Workflow、Temporal、数据库或 UI 业务语义。
5. Provider 只实现能力，不拥有平台级编排状态。
6. A2A 是独立能力和协议适配器，可以不依赖 Workflow、Temporal 或 Control Plane 单独运行。
7. Workflow、DAG、状态机和 Cron 都是可选 Coordinator。
8. Temporal 是 Durable Coordinator Provider，不是平台核心。
9. Profile 是模块组合和启动边界。
10. Durable Fact、Runtime Event 和 Decision Waterfall 使用不同事件语义。
11. V3 不保留 V2 API、数据库模型和双写兼容。
12. 所有 Provider 必须通过统一 Capability Contract Test，能力不足必须 fail closed。

## 结果

正面结果：

- A2A 可以独立发布；
- 不使用 Workflow 也能执行 Agent；
- 新增 Provider 不需要修改 Coordinator；
- 新增 Coordinator 不需要修改 Agent/Tool 能力；
- Temporal、PostgreSQL 和 FastAPI 变成可选依赖；
- Composition Kernel 可以独立构建、测试和发布；
- 可按 Profile 构建最小运行时。

代价：

- 需要重新定义现有模型和 API；
- 需要建立模块依赖检查；
- 需要同时维护 Capability、Provider 和 Consumer 契约；
- V2 的持久化数据不直接迁移。

## 明确不采用

- 不复制完整 Cordis 动态 Context；
- 不建设全局隐式 Service Locator；Kernel 只提供 Host-scoped 显式 Context；
- 不让 Kernel 直接知道 Agent、A2A、Workflow 或 Provider SDK；
- 不让 A2A Server 直接依赖 Codex 或 Temporal；
- 不让 PostgreSQL、Temporal 和 Provider Session 同时推进同一 Invocation；
- 不把 LLM 作为全局调度器。
