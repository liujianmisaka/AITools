# Multi-Agent Web V3

独立的 React/Vite 控制面前端，不被 multi-agent-v3 核心包导入。

安装依赖：npm install
开发启动：npm run dev

Vite 默认监听 127.0.0.1:5173，/api 请求代理到 127.0.0.1:8016 的 FastAPI
Control Plane。页面包含执行中心、委派状态、能力目录、服务管理、模板与实例、审批中心；委派状态页面使用
mcp-client/application 作为默认观察主体读取 /delegations，并以 SSE 为主、定时刷新为后备同步状态；可通过
VITE_DELEGATION_ACTOR_ID 和 VITE_DELEGATION_ACTOR_KIND 覆盖。服务管理页面读取 `/services`
并通过固定服务 ID 启动或停止已登记的本地服务；创建任务时前端读取 `/models`
目录，模型和推理等级来自已注册 Provider。这里的服务管理范围属于 Control Plane，主要负责
下游 A2A 服务；启动 Control Plane 自身、主 Web 和完整 AITools 服务组由独立的
`multi-agent-service-web` 引导管理面负责。生产构建使用 npm run build。

当委派确实进入 `reconciliation_required` 时，详情页提供带 revision 栅栏的人工结算表单；用户应先
核对同页的 Agent 会话，再确认完成、失败或取消，并填写核对依据。普通 Provider 完成事件不会要求
用户执行该操作。

委派页采用任务列表 + 完整内容区，不使用委派抽屉。内容区合并 Interaction Channel 和 Agent
Session Event：委托者指令、Agent Markdown/LaTeX 输出、可回答问题、工具/命令/文件事件都可回放。
`continuable` 委托即使当前 Activation 已结束也会保持消息流连接，可以直接发送下一轮；活动
Activation 支持 `append` 或带 Activation 栅栏的 `interrupt_continue`。回答 Agent 问题时页面会
自动提交 `answer`、`reply_to` 和 `correlation_id`。可选模型与 effort 只影响下一 Activation，
必须成对填写。页面不会传入 `cwd` 或 `sandbox`，这些可信执行上下文仍由 Control Plane 从原委托
恢复并重新执行允许路径校验。
Coordinator 页面通过独立的 /coordinator-api 代理访问由统一服务平台启动的 multi-agent-coordinator（默认 http://127.0.0.1:8020），展示持久会话、PlanGraph、审批和可见事件。开发时可使用 VITE_COORDINATOR_API_PROXY_TARGET 覆盖 Coordinator 地址；页面通过 SSE 游标恢复历史事件，不展示 MAF 隐藏思维内容。
