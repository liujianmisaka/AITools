# Multi-Agent Service Web

AITools 层的独立引导管理面，用于统一查看、启动和停止 Multi-Agent V3 的本地服务。
它不依赖已经运行的 Control Plane，而是由两个外围组件组成：

- AITools Management API，默认监听 `127.0.0.1:8014`；
- React/Vite 管理页面，默认监听 `127.0.0.1:5174`，并将 `/api` 代理到 Management API。

Management API 复用 V3 公开的 `misaka_service_runtime.ServiceManager`，但仍位于 AITools
外围管理层。V3 核心、Provider 和 Control Plane 都不会反向依赖本项目。

## 服务所有权

统一目录按真实生命周期区分服务：

- `control-plane`：由 AITools Management API 直接托管；
- `web-v3`：由 AITools Management API 直接托管，启动时自动先启动 Control Plane；
- `a2a-node`、`a2a-agent-host`：由 Control Plane 的静态服务目录托管，Management API
  通过 HTTP 代理启停和状态；
- `multi-agent-mcp`：由 Codex 或其他 MCP 客户端按需启动的 stdio 进程，只展示生命周期
  归属，不伪装成共享常驻服务。

页面只提交固定服务 ID、动作和当前 epoch，不接受任意命令、工作目录、环境变量或进程参数。
停止 Control Plane 前会先校验 epoch，再停止下游 A2A 服务和主 Web，避免陈旧页面误停新一代进程。

服务组：

- `core`：Control Plane 与主 Web；
- `all`：核心服务与 Control Plane 发布的全部可控下游服务。

由于 Control Plane 是下游服务的依赖，停止 `core` 时也会先收口下游服务。

## 准备环境

首次运行需要准备 V3 Python 环境和两个前端的依赖：

~~~powershell
cd D:\dev\AITools\multi-agent-v3
uv sync --all-packages

cd ..\multi-agent-web-v3
npm ci

cd ..\multi-agent-service-web
npm ci
~~~

## 启动与停止

在 AITools 根目录只启动管理面：

~~~powershell
.\start-multi-agent-service-web.ps1
~~~

此时只启动 `8014` 的 Management API 和 `5174` 的管理页面。打开
`http://127.0.0.1:5174` 后，可选择“启动核心”或“启动全部”。

一次启动管理面、Control Plane 和主 Web：

~~~powershell
.\start-multi-agent-v3-dev.ps1
~~~

停止核心服务但保留管理面：

~~~powershell
.\stop-multi-agent-v3-dev.ps1
~~~

停止全部业务服务以及 Management API、管理页面：

~~~powershell
.\stop-multi-agent-service-web.ps1
~~~

真实 Codex Profile 必须显式提供 Codex Home 和工作区白名单：

~~~powershell
.\start-multi-agent-service-web.ps1 `
  -Profile codex `
  -CodexHome C:/Users/<user>/.codex `
  -WorkspaceRoot D:/dev/AITools/multi-agent-v3
~~~

端口也可以独立覆盖：

~~~powershell
.\start-multi-agent-service-web.ps1 `
  -ManagementPort 8114 `
  -FrontendPort 5274 `
  -ControlPlanePort 8116 `
  -MainWebPort 5273
~~~

## Management API

- `GET /configuration`：读取当前 Profile、端口和工作区配置；
- `GET /services`：读取 AITools、Control Plane 和客户端生命周期的统一服务目录；
- `POST /services/{service_id}/start?epoch={epoch}`：启动单个服务；
- `POST /services/{service_id}/stop?epoch={epoch}`：停止单个服务；
- `POST /groups/core/start|stop`：启停核心服务组；
- `POST /groups/all/start|stop`：启停全部服务组；
- `GET /health`、`GET /ready`：Management API 探针。

默认状态文件位于 AITools 根目录的
`.data/multi-agent-v3/control-plane-{profile}.jsonl`；启动器日志和精确 PID/启动时间清单位于
`.tmp/multi-agent-service-web-runtime`。

## 开发验证

~~~powershell
cd D:\dev\AITools\multi-agent-service-web
..\multi-agent-v3\.venv\Scripts\pytest.exe -q
..\multi-agent-v3\.venv\Scripts\ruff.exe check backend tests
..\multi-agent-v3\.venv\Scripts\ruff.exe format --check backend tests
..\multi-agent-v3\.venv\Scripts\basedpyright.exe -p pyproject.toml
npm run build
~~~
