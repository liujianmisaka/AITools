# V3 Application Profile 目录

> 本文定义目标 Profile 的职责和绑定关系，不定义具体 Python 包。

Profile 是一组明确的 Composition Snapshot，不是新的能力实现。Profile 负责选择模块、Provider、Coordinator、Persistence、Transport 和长期服务。

## 1. standalone-agent

```text
Composition Kernel
Execution Runtime
Agent Capability
Session Log
Workspace / Process / Artifact
Fake 或真实 Agent Provider
```

用途：不依赖 A2A、Workflow、Temporal、Control Plane 或 Web 完成一次本地 Agent Execution。

## 2. a2a-node

```text
Composition Kernel
A2A Contract
A2A Task Runtime
A2A HTTP/SSE Transport
Task Projection Store
Delegation Provider
```

用途：独立发布和消费 A2A Task。

A2A Task 是协议包装，Execution Fact 仍由 Execution Runtime 或 Delegation Runtime 所有。

## 3. agent-host

```text
Composition Kernel
Execution Runtime
Agent Capability
Session Log
Workspace / Process / Sandbox
Agent Provider
```

用途：提供可被本地、远程或 A2A Delegation 调用的 Agent Execution。

## 4. a2a-agent-host

```text
a2a-node
agent-host
Delegation / Continuation implementation
A2A Delegation Provider
```

用途：将 Agent Host 的能力通过 A2A 暴露。

A2A Transport 不得直接调用 Agent Provider SDK。

## 5. local-delegation

```text
Composition Kernel
Execution Runtime
Delegation Seam implementation
Session Log
Interaction Channel
Policy / Decision Gate
Workspace / Artifact
Local Delegation Gateway
Local、In-process 或 CLI Provider
```

用途：为本机调用方提供一次性或可持续的 Delegation。调用方可以是人、应用或 Agent；Profile 不假设调用方的具体 Provider。

该 Profile 不要求 Workflow、DAG、Temporal 或 A2A。需要远程传输时，可以把 Delegation Provider 或 Gateway 替换为 A2A/HTTP/其他 Transport。

## 6. durable-agent

```text
Execution Runtime
Session Log
Execution Fact Store
Projection Store
Durable Coordinator
Reconciliation Service
```

用途：长任务、重试、恢复、Activation Reconciliation 和持久化审计。

Profile 必须声明：

- Execution Fact 的唯一事实源；
- Session Log 的唯一事实源；
- Projection 的来源和 watermark；
- Durable Coordinator 与 Execution Runtime 的责任边界。

## 7. control-plane

```text
Execution Runtime
Direct / Reactive / Queue Coordinator
Event Sources
Human Approval
Settings / Credential / Authorization
Job / Task Projection
Managed Service Runtime
FastAPI Transport
```

用途：本地 API、事件触发、应用投影、审批和服务管理。

Control Plane 不能直接调用 Provider SDK、subprocess 或数据库内部 API。

## 8. control-plane-workflow

```text
control-plane
DAG / State Machine / Workflow Coordinator
```

Workflow 是可选组合方式。删除该 Profile 不得影响 standalone-agent、agent-host、a2a-node 或 Delegation。

## 9. service-host

```text
Composition Kernel
Managed Service Runtime
Service Catalog
Health / Process / Cleanup Providers
```

用途：独立管理长期运行的 A2A Node、MCP Server、Poller、Worker 或其他本地服务。

Service Runtime 不推进 Invocation 或 Task 状态。

## 10. Profile 约束

- Profile 必须显式声明所有必需 Port 和 Provider；
- Profile 必须生成 Composition Snapshot；
- Profile 必须声明事实所有者和 Projection 关系；
- Profile 必须声明 owner、scope 和权限边界；
- 如果 Profile 支持 Continuation，必须声明 Session Log、Interaction Channel、消息事实来源和恢复策略；
- 如果 Profile 支持 Delegation，必须声明 parent/child owner、Decision Gate、深度/并发/资源预算和报告来源；
- Profile 停止时按资源 owner 逆序释放；
- Profile 不得让底层模块反向依赖自身；
- Profile 可以没有 Coordinator、Web、Temporal、PostgreSQL 或 Managed Service；
- Profile 不得将当前实现包名写入领域 Contract。
