# ADR-0005：V3 执行适配器与会话观察边界

- 状态：已接受
- 日期：2026-08-27

## 背景

Coordinator 需要驱动 Multi-Agent V3 的委派生命周期，但不能直接依赖 V3 的 Control Plane、Provider、
Invocation 或持久化实现。仅依赖 MCP 工具还不足以读取历史会话事件和订阅会话流，因此需要一个位于
Coordinator 基础设施层的适配器，统一执行命令和会话观察的错误、类型与边界。

## 决策

1. V3ExecutionGateway 只通过已注册的工具调用器访问 V3 MCP，覆盖委派、查询、等待、列表、消息、
   取消、对账和执行选项查询。
2. 请求和响应先映射到 Coordinator 自己的不可变契约对象；V3 的 wire 字段不会泄漏到领域层。
3. MCP 调用失败分为工具不可用、工具执行失败和协议错误三类稳定异常；底层异常作为 cause 保留，
   不把内部错误文本直接暴露给上层用户。
4. V3SessionGateway 只使用 V3 的公开 HTTP 接口读取会话快照、历史事件和 SSE 会话流；它不导入
   V3 内部模块。
5. 会话观察强制校验请求的 delegation_id、事件序号单调性、SSE 事件类型和结束序号；委派 ID 作为
   单一路径段进行 URL 编码，避免 ID 中的斜杠破坏路由。
6. 所有有界参数在适配器入口校验：等待超时为 0—300000 毫秒，列表限制为 1—100，事件游标和结束
   序号必须是正整数且拒绝 bool。
7. 会话流以 snapshot、event、end 三种显式 envelope 暴露；实时输出由 V3 会话事件承载，Coordinator
   不复制 Provider 会话状态。

## 结果

Coordinator 可以在不污染领域层的前提下切换 V3 MCP 来源、读取历史委派并订阅实时会话。V3 仍是
Delegation、Activation、Invocation、Worker Session、取消和对账事实的唯一所有者；适配器只负责协议
转换、边界校验和错误归一化。
