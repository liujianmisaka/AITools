# Multi-Agent Service Web

AITools 层的独立本地服务管理页面。它只通过 HTTP 调用 Multi-Agent V3 Control Plane，
不导入 Runtime、Provider、持久化或进程管理模块。

页面提供：

- 读取 GET /services 服务目录并每 2 秒刷新；
- 展示服务名称、ID、分类、endpoint、状态、PID、epoch、时间、错误和最近日志；
- 通过 POST /services/{service_id}/start?epoch={current_epoch} 启动服务；
- 通过 POST /services/{service_id}/stop?epoch={current_epoch} 停止服务；
- 在 epoch 过期时展示 Control Plane 的 fencing 错误并重新同步状态。

页面不会接收任意命令、工作目录、环境变量或进程参数。可操作服务及其启动命令完全由
Control Plane Profile 的静态服务目录决定。

## 开发运行

先启动带服务目录的 V3 Control Plane，然后执行：

~~~powershell
npm install
npm run dev
~~~

默认页面地址为 http://127.0.0.1:5174，Vite 将 /api 代理到
http://127.0.0.1:8016。需要连接其他 Control Plane 地址时，在启动前设置
VITE_API_PROXY_TARGET。

也可以在 AITools 根目录独立启动和停止该页面：

~~~powershell
.\start-multi-agent-service-web.ps1
.\stop-multi-agent-service-web.ps1
~~~

指定端口或 Control Plane：

~~~powershell
.\start-multi-agent-service-web.ps1 -FrontendPort 5184 -ControlPlaneUrl http://127.0.0.1:8127
~~~

生产构建：

~~~powershell
npm run build
~~~
