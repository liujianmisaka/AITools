# ADR-0001：以 Temporal 构建本机持久化编排平台

- 状态：Accepted
- 日期：2026-08-16

## 背景

V1 已具备 DAG、持久化、Trigger、Schedule 和多个 Provider 适配，但可靠执行、恢复、Timer、状态机和调度生命周期主要由项目自行维护。项目允许完全重构，并要求长期稳定性优先于兼容成本。

实际部署边界进一步收缩为：可信局域网内无登录使用，所有 Agent、工作区和核心服务位于同一台 Windows 主机。

## 决策

1. Temporal 是唯一 Durable Workflow 引擎，负责 Workflow History、Timer、Signal、Update、Activity、Retry、Cancel 和 Worker 调度。
2. PostgreSQL 是模板、配置、Inbox、Outbox、投影和执行租约的权威来源，但不推进 Workflow 节点。
3. V2 使用独立目录、API 和数据库，不保留 V1 兼容层或双写。
4. 所有 Agent 通过本机 `AgentRuntime` 执行，不支持 A2A、远程 Agent 或跨主机 Worker。
5. Control API、Temporal、PostgreSQL 和 Worker 只监听回环地址；后续只有同源 Web/BFF 可以对局域网开放。
6. 不提供登录、账号或角色。任何能够访问 Web/BFF 的局域网用户都具有相同操作能力。
7. Provider 凭据由本机 Agent/CLI 自己管理，不进入平台模板、API、数据库或 Workflow input。

## 结果

- 项目不再自行实现耐久 Timer、执行恢复和 Workflow checkpoint。
- Temporal Workflow 必须保持确定性，所有 I/O 都在 Activity 中完成。
- PostgreSQL 投影可能短暂落后于 Temporal History，需要幂等投影和对账。
- 本机部署仍包含 Temporal 与 PostgreSQL 两个基础服务，运维复杂度高于 SQLite 单进程方案。
- 无登录显著简化产品，但局域网边界失守时不存在用户级授权防线。
