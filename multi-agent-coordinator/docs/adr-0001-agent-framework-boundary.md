# ADR-0001：Microsoft Agent Framework 与 V3 的职责边界

- 状态：已接受
- 日期：2026-08-27

## 背景

Multi-Agent V3 已经拥有 Provider-neutral 的 Delegation、Activation、Invocation、Worker Session、
权限、持久化、Temporal 恢复、服务管理和对账能力，但缺少能够持续对话、自主规划和使用通用工具
生态的认知协调层。

Microsoft Agent Framework 提供 Agent、AgentSession、上下文管理、MCP 工具和多 Agent 编排能力。
完整元包同时引入大量与本项目无关或仍处于预览阶段的连接器，并会改变 V3 共享运行环境中的
FastAPI、MCP 和 OpenTelemetry 等依赖版本。

## 决策

1. Coordinator 作为独立 AITools 服务和独立 UV 项目存在，不加入 `multi-agent-v3` workspace。
2. Coordinator 选择性依赖 MAF Core、OpenAI-compatible 连接器、Orchestrations 和 MCP。
3. MAF `AgentSession` 只保存用户与 Coordinator 的认知会话。
4. V3 Session 继续保存委托者与 Worker Agent 的原生会话和实时事件。
5. Coordinator 只能通过 V3 公开 MCP/HTTP 接口创建、查询、继续、取消或对账委派。
6. Coordinator 不导入 Codex、Claude、A2A Provider 或 V3 持久化实现。
7. MAF Workflow/Magentic 只用于一次 Coordinator 激活内的计划、审查和重规划，不拥有长期任务事实。
8. V3 Delegation/Invocation 和现有 Temporal Profile 继续拥有跨 Provider 长任务执行事实。
9. 不启用 MAF Durable Task，避免与 Temporal 形成第二套持久执行状态机。
10. 权限扩张、工作区写入、破坏性操作和人工对账仍由 V3 Decision Gate/Policy 执行。

## 依赖策略

阶段 0 将 MAF 限制在当前已验证的小版本范围：

- `agent-framework-core>=1.15,<1.16`
- `agent-framework-openai>=1.14,<1.15`
- `agent-framework-orchestrations>=1.1,<1.2`
- `mcp>=1.24,<2`

依赖升级必须重新执行 Coordinator 静态检查、单元测试、技术探针，以及 V3 全量回归测试。

## 结果

该边界允许 Coordinator 复用 MAF 的 Agent 与工具生态，同时避免 MAF Provider、Workflow 或
Durable Runtime 成为 V3 的第二执行事实源。代价是需要维护一个明确的 V3 MCP/HTTP 适配层，以及
Coordinator Plan 与 V3 Delegation 之间的持久关联。
