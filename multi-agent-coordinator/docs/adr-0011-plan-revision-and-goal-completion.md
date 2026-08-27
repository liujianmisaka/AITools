# ADR-0011：计划修订与目标完成

## 决策

1. Coordinator 计划使用稳定 `plan_id`，每次创建或修订都会追加一个单调的 `PlanRevision`。
2. 修订可以改变尚未产生 V3 执行引用的节点；已经绑定 `ExecutionReference` 的节点及其任务意图不可
   改写，只能保留、验收、重试或取消。
3. `PlanGraph` 随修订生成新的 revision；它只表达 Coordinator 依赖，不成为 V3 执行事实。
4. `accept_result` 只把处于 `REVIEW_REQUIRED` 的节点标记为 `ACCEPTED`；`complete_goal` 要求
   所有节点已接受，再将 Plan 和 Goal 一起置为完成。
5. 目标、计划和 V3 Delegation 的状态继续分开持有，重启时从 JSONL 记录恢复。
