# Multi-Agent Web V3

独立的 React/Vite 控制面前端，不被 multi-agent-v3 核心包导入。

安装依赖：npm install
开发启动：npm run dev

Vite 默认监听 127.0.0.1:5173，/api 请求代理到 127.0.0.1:8016 的 FastAPI
Control Plane。生产构建使用 npm run build。
