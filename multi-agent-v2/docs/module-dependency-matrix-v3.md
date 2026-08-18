# V3 模块依赖矩阵

## 1. 依赖方向

~~~text
Profiles
  -> Coordinators
  -> Capability Providers
  -> Invocation Runtime
      -> Composition Kernel
      -> Contracts
~~~

Provider 和 Coordinator 都可以依赖 Invocation Runtime；Invocation Runtime 依赖 Composition Kernel，Kernel 不依赖 Invocation Runtime。

## 2. 模块矩阵

| 模块 | 可依赖 | 禁止依赖 |
|---|---|---|
| contracts | 标准库、Pydantic（可选） | FastAPI、Temporal、SQLAlchemy、Provider SDK、应用包 |
| kernel | contracts、标准库 | Agent、A2A、Workflow、Temporal、数据库、Provider SDK、UI |
| runtime | contracts、kernel | Workflow DSL、Control Plane、具体 Provider |
| composition | contracts、kernel、runtime | 具体业务 Coordinator 内部状态 |
| capability-agent | contracts、kernel、runtime | Temporal、Control Plane |
| capability-a2a | contracts、kernel、runtime | Codex SDK、Workflow DSL、Control Plane |
| capability-tool | contracts、kernel、runtime、policy | Temporal、UI |
| capability-policy | contracts、kernel | Provider SDK、FastAPI |
| capability-artifact | contracts、kernel | Workflow DSL |
| provider-codex | Agent Capability、Process、Policy、Workspace | Control Plane、Coordinator |
| provider-a2a-http | A2A Contracts、HTTP Client | Temporal、Control Plane |
| coordinator-dag | contracts、runtime | Codex SDK、A2A HTTP 实现 |
| coordinator-temporal | contracts、runtime、Temporal SDK | A2A Server、Provider SDK |
| persistence-postgres | Contracts、Persistence Ports | Temporal 内部表、UI |
| profile-control-plane | 所需 Provider、Coordinator、Transport | 被底层能力反向依赖 |

## 3. 机械检查

未来 CI 必须检查：

- 禁止循环依赖；
- 禁止底层包导入 Profile；
- A2A Contracts 不得导入 FastAPI；
- Kernel 不得导入任何业务能力或基础设施；
- Runtime 不得导入 Temporal；
- Coordinator 不得直接导入 Provider SDK；
- Control Plane 不得直接导入 Codex SDK；
- 每个 Provider 都有对应 Contract Test。
