# ADR-0007：Coordinator 决策执行闭环

- 状态：已接受
- 日期：2026-08-27

## 背景

Phase 2 只负责让 MAF Agent 产生一个结构化 CoordinatorDecision，Phase 4 只负责访问 V3，Phase 5
只负责计算 PlanGraph 的依赖前沿。若没有应用层闭环，模型决策不会改变 Coordinator 计划，也不会
真正创建或观察 V3 委派。

## 决策

1. CoordinatorOrchestrator 负责一次有界 activation：调用 MAF Agent、读取当前领域事实、应用决策，
   直到返回用户可见结果、等待/审查状态或达到 max_decision_steps。
2. CREATE_PLAN 只创建 Coordinator Plan、PlanGraph 和节点依赖；DELEGATE 才通过
   V3ExecutionGateway 创建执行事实。
3. 委派请求使用节点选择、工作目录、能力、幂等键、会话 ID 和决策引用；`parent_task_id` 只形成
   Coordinator PlanGraph 的执行依赖，不映射为 V3 `parent_delegation_id`。Coordinator 直接派遣的
   每个计划节点都是顶层委派；真正的 V3 子委派由仍处于活动状态的受托 Agent 发起。不把 Provider
   SDK 对象或 V3 内部状态写入 Coordinator。
4. 只有 PlanGraph.ready_node_ids 返回的节点才允许派遣，保证依赖未完成时不会启动子任务；独立节点
   可以在同一 activation 中连续派遣。
5. WAIT 使用有界 timeout_ms；非终态返回 WAITING，完成或需要对账的 V3 委派转为 Coordinator 的
   REVIEW_REQUIRED，失败/取消映射到 PlanNode 的失败或取消状态。
6. 每次 activation 返回新的 CoordinatorSession、MAF AgentSession、决策列表和 V3 快照，调用方负责
   持久化这两个会话对象；CoordinatorOrchestrator 不建立第二套持久执行引擎。

## 结果

Coordinator 现在具备从认知决策到计划变更、委派创建和有界观察的应用层闭环。MAF 仍只拥有认知
会话，V3 仍是 Delegation/Activation/Invocation 的事实源，PlanGraph 只负责调度依赖判断。
