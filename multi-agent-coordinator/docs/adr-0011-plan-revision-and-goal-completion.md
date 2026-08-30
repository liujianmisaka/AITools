# ADR-0011：计划修订与目标完成

## 决策

1. Coordinator 计划使用稳定 `plan_id`，每次创建或修订都会追加一个单调的 `PlanRevision`。
2. 修订可以改变尚未产生 V3 执行引用的节点；已经绑定 `ExecutionReference` 的节点及其任务意图不可
   改写，只能保留、验收、重试或取消。
3. `PlanGraph` 随修订生成新的 revision；它只表达 Coordinator 依赖，不成为 V3 执行事实。
4. `accept_result` 把处于 `REVIEW_REQUIRED` 的节点标记为 `ACCEPTED`；接受最后一个节点时，
   Orchestrator 确定性地同时完成 Plan 和 Goal，不再要求模型追加一次 `complete_goal` 决策。
   `complete_goal` 仍保留为显式决策入口，但不是人工验收后的必需步骤。
5. 取消 Goal 前，应用服务必须先取消仍处于 `DELEGATED` 或 `AWAITING_EVENT` 的 V3
   Delegation；全部执行事实收口后，Plan 和 Goal 一起进入取消终态。取消失败时不得提前终止 Goal。
6. 目标、计划和 V3 Delegation 的状态继续分开持有，重启时从 JSONL 记录恢复。归档旧记录时，
   允许在不存在活动 Delegation 的前提下收口遗留的“Goal 已失败或取消、Plan 仍非终态”状态。
