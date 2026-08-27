# ADR-0004：MCP 工具注册与调用边界

- 状态：已接受
- 日期：2026-08-27

## 背景

Coordinator 需要使用 V3 MCP，并为未来的文件、搜索和数据库 MCP 保留扩展能力。如果直接把 MCP
函数交给 MAF Agent，模型调用会绕过统一白名单、参数校验、超时和审计；如果在 Coordinator 中
硬编码 V3 工具名，又会让认知层依赖具体业务实现。

## 决策

1. `ToolSource` 是 MCP Registry 的唯一来源接口，负责发现、调用和关闭一组工具。
2. `MAFMCPToolSource` 适配 MAF stdio/Streamable HTTP MCP；其他 MCP 不需要修改 Registry。
3. MCP Source 使用服务端原始工具白名单，Registry 再使用暴露名称白名单，形成两层限制。
4. 能力分类由配置提供 `capability_id`，Registry 不硬编码 V3 工具名称。
5. Registry 发现结果形成原子 revision snapshot；单个来源失败只标记该来源不可用。
6. 同名工具冲突属于配置错误，拒绝发布新快照，不以来源顺序静默覆盖。
7. 调用前使用 Draft 2020-12 JSON Schema 校验参数；非法参数不会到达 MCP Server。
8. Registry 为 MAF 生成代理 `FunctionTool`，代理仍通过 Registry 调用，不能绕过策略和审计。
9. 审计只记录工具名、来源、参数名称、结果类别和耗时，不保存参数值或工具结果。
10. 超时、来源失败、未发现和参数错误转换为稳定的 Registry 错误。

## 结果

MAF Agent 可以使用统一工具生态，同时 Registry 仍是唯一执行入口。V3 MCP 只是一种配置的
ToolSource，不会进入 Coordinator 领域层；不可用来源可以被页面展示并由 Agent 选择降级方案。
