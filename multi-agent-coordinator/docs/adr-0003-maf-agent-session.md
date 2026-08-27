# ADR-0003：MAF Agent 与认知会话持久化

- 状态：已接受
- 日期：2026-08-27

## 背景

Coordinator 需要持续接收用户消息并跨进程恢复认知上下文。MAF `AgentSession` 支持序列化，但
OpenAI Responses 客户端默认优先使用服务端存储。OpenCodex 等 OpenAI-compatible 网关未必完整
实现服务端 conversation/response 恢复语义，同时服务端 ID 也不能替代本地可审计会话快照。

## 决策

1. Coordinator 使用 MAF `Agent` 和 `OpenAIChatClient`，支持官方 OpenAI 或自定义 `base_url`。
2. Agent 固定 `store=False`，让 MAF 自动使用本地 HistoryProvider，并将消息历史写入
   `AgentSession.state`。
3. MAF Session 只承载用户与 Coordinator 的认知对话；V3 Worker Session 继续承载委派会话。
4. MAF Session 使用其公开 `to_dict`/`from_dict` 接口持久化，不解释框架内部 history 结构。
5. 每个 Coordinator 激活使用单调 decision step；只有结构化决策验证成功后才推进 step。
6. 单次激活受 `max_decision_steps` 限制，单次模型输出受 `max_output_tokens` 和请求超时限制。
7. 模型必须返回严格 JSON Schema；进入领域逻辑前转换为 `CoordinatorDecision` 并校验动作不变量。
8. 模型连接错误和超时归一化为应用层错误，不向领域层泄漏 MAF/OpenAI 异常。

## 结果

Coordinator 可以使用一个可恢复的本地认知会话持续决策，同时不会把服务端 response ID 当作
授权边界或唯一历史来源。后续 MCP 工具只会挂载到该 Agent，执行事实仍由 V3 保存。
