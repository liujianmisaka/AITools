# V3 Profile 目录

Profile 是运行时组合，不是新的业务核心。每个 Profile 声明模块、Provider、Transport、Persistence 和 Coordinator。

## 1. a2a-node

~~~text
a2a-contracts
capability-kernel
a2a-server
a2a-http
task-store-memory
direct-coordinator
fake-agent-provider
~~~

用途：协议开发、Fake 测试和本机互操作。

## 2. agent-host

~~~text
agent-capability
capability-kernel
codex-provider 或 fake-provider
workspace-local
process-local
artifact-filesystem
session-jsonl
~~~

用途：不依赖 A2A 和 Workflow 的本地 Agent 执行。

## 3. a2a-agent-host

~~~text
a2a-node
agent-host
capability-kernel
a2a-agent-task-handler
~~~

用途：通过 A2A 暴露本地 Agent 能力。

## 4. durable-agent

~~~text
agent-host
capability-kernel
postgres-persistence
temporal-coordinator
~~~

用途：长任务、恢复、重试和 durable execution。

## 5. control-plane

~~~text
durable-agent
capability-kernel
event-sources
human-approval
fastapi-transport
web-bff
~~~

用途：当前 UI、模板、实例、事件和运维控制。它是应用 Profile，不是基础能力。

## 6. Profile 约束

- Profile 必须显式列出所有 Provider；
- 未绑定必需能力时启动失败；
- Profile 不得改变底层 Contract 语义；
- Profile 停止时必须按 owner 逆序释放资源；
- Profile 可以没有 Coordinator；
- Profile 可以没有 Web、Temporal 或 PostgreSQL。
