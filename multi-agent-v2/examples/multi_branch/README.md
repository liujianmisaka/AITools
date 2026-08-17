# 多分支测试执行流

`workflow.json` 是可直接导入的 V2 工作流模板示例，覆盖：

- `prepare` 根节点；
- `agent_check` 与 `policy_check` 两条并行分支；
- `checks_joined` 全量汇合；
- `route` 条件判断；
- `ship` 与 `review` 两个互斥终态分支。

示例使用 `fake/model` 测试目录语义，不会调用真实模型。仓库测试会分别以
`release=true` 和 `release=false` 运行 Temporal Workflow，并对两条路径及 History
replay 进行验证。
