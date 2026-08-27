# ADR-0006：Coordinator PlanGraph 任务图

- 状态：已接受
- 日期：2026-08-27

## 背景

Coordinator 的 Plan 已经描述任务节点和节点生命周期，但节点之间仍是平面集合，无法表达“先完成
研究再汇总”或“两个独立任务可以并行”这类调度约束。执行事实仍由 V3 Delegation/Activation
拥有，因此图能力必须只描述认知计划的依赖关系，不能复制执行状态。

## 决策

1. PlanGraph 是独立的 Coordinator 领域对象，以 plan_id 和不可变 PlanDependency 边集合表示 DAG。
2. 依赖边只引用 node_id；Provider、Delegation、Invocation 和 Session ID 不进入图结构。
3. 构造和反序列化时拒绝自依赖、重复边和环；绑定到 Plan 时再校验 plan_id 及所有节点引用。
4. topological_order 返回按 Plan 节点顺序稳定的拓扑序；ready_node_ids 只返回当前为 READY 且所有
   前置节点为 ACCEPTED 的节点，作为应用层并行派遣的候选集合。
5. 图的 revision 和时间戳随边增删单调推进；PlanGraph 可以独立 JSON 序列化，并可通过 CoordinatorSession
   的可选 plan_graph 字段与认知会话一起恢复。

## 结果

Coordinator 具备可验证的 DAG 依赖和并行前沿计算能力，同时保持 PlanNode 状态与 V3 执行状态的边界。
后续应用层可以把 ready_node_ids 交给 V3ExecutionGateway，并根据 Delegation 事件更新 PlanNode，
而不需要在图模型中新增 Provider 或运行时逻辑。
