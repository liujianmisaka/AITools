# V3 Capability-First 实施阶段

V3 允许破坏性重构，不做 V2 兼容迁移。实现顺序必须先完成独立能力，再实现 Coordinator 和 Control Plane。

## M0：术语、ADR 和依赖基线

产出：

- architecture-capability-first-v3.md；
- adr/0002-capability-first-architecture.md；
- capability-seams-v3.md；
- kernel-design-v3.md；
- module-dependency-matrix-v3.md；
- profile-catalog-v3.md；
- import graph 和禁止依赖检查。

门禁：新代码不再以 Workflow 作为底层依赖。

## M1：Contracts

抽取：

- CapabilityDescriptor；
- InvocationRequest/Result/Event；
- Activation；
- SessionRef；
- ArtifactRef；
- PolicyDecision；
- ResourceLease；
- Provider/Consumer Protocol。

门禁：Contracts 可独立构建和测试。

## M2：Composition Kernel

实现独立的 Python Composition Kernel：

- ModuleManifest；
- HostContext；
- ServiceRegistry；
- CapabilityDirectory；
- LifecycleScope；
- EventDispatcher；
- Profile Loader；
- 依赖拓扑、冲突和版本验证；
- attach/start/stop/dispose 回滚。

门禁：Kernel 可独立构建、测试和停止，不依赖 Agent、A2A、Workflow、Temporal 或数据库。

## M3：Invocation Runtime

实现：

- preflight；
- capability negotiation；
- lifecycle；
- cancellation；
- idempotency；
- event normalization；
- resource ownership；
- reconciliation；
- invariant registry。

门禁：Fake Provider Contract Test 通过，且无需 Temporal。

## M4：Agent 和基础能力

实现：

- Fake Agent；
- Codex Agent Provider；
- Tool Provider；
- Workspace；
- Process；
- Policy；
- Artifact；
- Session。

门禁：本地 Agent Host 可以不启动 Workflow 或 Control Plane 完成一次调用。

## M5：Standalone A2A

实现：

- A2A Contracts；
- A2A Client；
- A2A Server；
- HTTP/SSE Transport；
- Memory Task Store；
- Fake TaskHandler；
- A2A Contract Test。

门禁：A2A-only Profile 独立启动、执行、查询、取消、订阅并清理。

## M6：Direct/Reactive/Queue Coordinator

在没有 Workflow DSL 的情况下实现：

- 单次调用；
- 事件触发；
- 后台队列；
- 重试和取消。

门禁：Coordinator 只依赖 Invocation Runtime，不依赖具体 Provider。

## M7：Durable Adapter

实现：

- JSONL Event Log；
- PostgreSQL Store；
- Temporal Coordinator；
- durable Job Registry。

门禁：每个 Profile 明确唯一事实源，不出现双状态推进。

## M8：DAG/State Machine/Workflow

Workflow 在此阶段才实现，作为可选 Coordinator。

门禁：删除 Workflow Coordinator 不影响 Agent、A2A、Tool 和 Direct Profile。

## M9：Control Plane 和 Web Profile

最后重做：

- Control API；
- 模板和实例；
- 事件触发；
- 审批；
- Web UI。

门禁：Control Plane 只能通过公开 Capability/Coordinator 接口运行，不能直接导入 Provider SDK。

## 总验收

至少通过：

1. a2a-node 独立 Profile；
2. agent-host 独立 Profile；
3. a2a-agent-host 组合 Profile；
4. durable-agent Profile；
5. control-plane Profile；
6. 所有 Provider Contract Test；
7. 真实入口测试；
8. 崩溃、取消、重试和资源清理测试；
9. 模块依赖和禁止依赖检查；
10. 测试结束后无残留进程、端口、工作区和后台任务。
