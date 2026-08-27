# ADR-0002：Coordinator 领域状态与 V3 执行事实分离

- 状态：已接受
- 日期：2026-08-27

## 背景

Coordinator 需要保存目标、计划、认知会话关联和事件游标，但 V3 已经拥有 Delegation、
Activation、Invocation、Worker Session 及其完整状态。如果 Coordinator 复制这些状态，会形成两个
可写事实源，并在异步事件、进程重启和人工对账时产生冲突。

## 决策

1. Coordinator 领域层只使用 Python 标准库，不导入 MAF、V3、Provider SDK、FastAPI 或数据库。
2. `CoordinatorSession` 是用户认知会话的聚合根，只保存 `cognitive_session_id`，不保存或解释 MAF
   `AgentSession` 的内部结构。
3. `ExecutionReference` 只保存 V3 的 Delegation、Activation、Invocation 和 Worker Session ID；
   Worker Session ID 可以在当前 Invocation 结束后继续存在，用于恢复或追加消息。
4. `PlanNodeStatus` 表示 Coordinator 的编排阶段，不表示 V3 Invocation 或 Provider 的执行状态。
5. V3 状态变化通过 `CoordinatorEvent` 引用进入领域层；事件不复制 V3 返回内容或状态快照。
6. 计划和会话使用单调 revision，拒绝旧快照覆盖新快照。
7. 事件的 `occurred_at` 与处理时间分离，允许可靠处理异步到达的事件，同时保持事件游标单调。
8. 会话 JSON 使用显式 `schema_version`；未知版本直接拒绝，不做隐式兼容转换。

## 结果

领域对象可以独立测试、序列化和恢复。后续 MAF 会话存储、MCP 工具、Temporal 事件和数据库适配器
只能在应用层实现。Coordinator 可以根据 V3 事实重新规划，但不能成为 V3 执行状态的第二写入者。
