# ADR-0010：Coordinator 自主性预算与审批

## 背景

Coordinator 可以连续调用模型、创建多个委派并在事件到达后自动恢复。如果额度和权限只存在于模型
提示词中，模型输出错误、重启恢复或直接工具调用都可能绕过限制。自主性策略必须是应用层的确定性
门禁，并且不能成为第二套 V3 执行事实。

## 决策

1. `CoordinatorAutonomyPolicy` 由宿主配置，控制并行委派、委派总数、子委派深度、计划修订、节点
   重试、Coordinator 累计模型运行时间和模型激活次数。运行时间只累计实际模型决策阶段，不包含
   委派等待、人工验收等待或会话闲置。
2. 用量和审批保存在 `CoordinatorSession.autonomy` 中，随 JSONL 会话一起恢复。它只记录 Coordinator
   已使用的额度和用户授权，不复制 Delegation、Invocation 或 Provider 状态。
3. 超出额度、扩大 Provider/工作目录作用域、写工作区、破坏性操作和人工对账都会产生绑定到精确
   `action_key` 的审批请求。模型决策不能批准、拒绝或修改策略。
4. 只有外部应用 API/MCP `coordinator_resolve_approval` 可以解决审批，并要求调用方提交预期会话
   revision。授权在受保护操作成功后才标记为已消费；同一幂等动作恢复时不会产生第二个审批。
5. 受管 Host 不把 V3 控制工具直接注册给认知 Agent。Agent 只产生结构化决策，Orchestrator 通过
   `V3ExecutionGateway` 执行，确保所有委派都经过策略门禁。
6. 工具调用审计使用独立追加式 JSONL，只保存工具名、来源、参数名称、结果状态和时间，不保存参数
   值、模型输出或工具结果。
7. `multi-agent-service-web` 持久化全部额度配置，并从当前 Provider 和允许路径配置生成 Coordinator
   作用域；统一服务启动时传入独立 Coordinator Host。

## 结果

- Coordinator 无法通过提示词修改自己的权限或预算。
- 重启不会重置当前 schema 下的额度和待审批状态。
- V3 仍然负责最终工作区、Provider、Decision Gate 和执行事实校验。
- 用户批准预算超限表示仅允许对应的精确动作，不会永久放宽全局策略。
