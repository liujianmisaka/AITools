# Multi-Agent Web V3

独立的 React/Vite 控制面前端，不被 multi-agent-v3 核心包导入。

安装依赖：npm install
开发启动：npm run dev

Vite 默认监听 127.0.0.1:5173，/api 请求代理到 127.0.0.1:8016 的 FastAPI
Control Plane。页面包含执行中心、委派状态、能力目录、服务管理、模板与实例、审批中心；委派状态页面使用
mcp-client/application 作为默认观察主体读取 /delegations，每 2.5 秒刷新状态，并可通过
VITE_DELEGATION_ACTOR_ID 和 VITE_DELEGATION_ACTOR_KIND 覆盖。服务管理页面读取 `/services`
并通过固定服务 ID 启动或停止已登记的本地服务；创建任务时前端读取 `/models`
目录，模型和推理等级来自已注册 Provider。这里的服务管理范围属于 Control Plane，主要负责
下游 A2A 服务；启动 Control Plane 自身、主 Web 和完整 AITools 服务组由独立的
`multi-agent-service-web` 引导管理面负责。生产构建使用 npm run build。
