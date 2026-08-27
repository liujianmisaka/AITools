# ADR-0008：Coordinator 持续交互与对账入口

- 状态：已接受
- 日期：2026-08-27

## 背景

一次 Delegate 只创建一个 V3 委派，但真实工作可能需要补充上下文、打断后继续、取消，或在外部
副作用无法确认时人工对账。重复调用如果再次执行 delegate，会产生第二个写 Activation，破坏同一
Worker Session 的连续性。

## 决策

1. CoordinatorOrchestrator 通过节点已有的 ExecutionReference 定位 Worker Session，不重复创建
   Delegation。
2. send_message 默认使用 append；continue_node 明确使用 interrupt_continue；两者都把 V3 返回的
   MessageDispatchSnapshot 原样映射为应用结果。
3. cancel_node 通过 V3 cancel_task 执行取消，并依据返回快照更新 PlanNode；取消事实仍由 V3 所有。
4. reconcile_node 通过 V3 resolve_task_reconciliation 执行人工对账，完成或需要继续审查的结果进入
   REVIEW_REQUIRED，不自动伪造 ACCEPTED。
5. Worker Session ID 在当前 Invocation 结束后仍保留在 ExecutionReference 中，允许完成后追加消息、
   恢复或重新对账。

## 结果

Coordinator 具备显式的持续交互和人工对账入口，重复操作可以复用同一委派会话。消息、取消、对账
仍经过 V3 公共接口，不把 Provider 私有句柄或外部副作用状态复制到 Coordinator。
