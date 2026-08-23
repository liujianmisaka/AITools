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

页面只提交固定服务 ID、动作、当前 epoch 和结构化 Provider 配置，不接受任意命令、环境变量或
进程参数。Provider 配置只允许引用凭据环境变量，不允许把密钥作为 config override 持久化。
路径筛选只接受已存在的绝对目录，并由下一次启动的 Control Plane 强制执行。停止 Control
Plane 前会先校验 epoch，再停止下游 A2A 服务和主 Web，避免陈旧页面误停新一代进程。

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
`http://127.0.0.1:5174` 后，先在“运行配置与路径筛选”中添加一个或多个 Fake/Codex Provider，
填写各自的 Provider ID、Codex Home、配置覆盖和网络隔离声明，再配置可选的允许根路径。允许根路径
支持点击“选择文件夹”打开运行 Management API 的本机
目录对话框，也可以直接编辑文本；可重复选择多个目录。保存后再选择“启动核心”或“启动全部”。
允许根路径每行一个；
留空表示不筛选，MCP 可以为每次委派传入任意存在的绝对目录。

使用已经保存的配置一次启动管理面、Control Plane 和主 Web（开发快捷入口）：

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

真实 Codex Provider 的配置只在服务管理页面或 `PUT /configuration` 中维护，不再作为启动脚本
参数。Control Plane 运行期间配置为只读；需要修改时先在统一平台停止核心服务。

端口也可以独立覆盖：

~~~powershell
.\start-multi-agent-service-web.ps1 `
  -ManagementPort 8114 `
  -FrontendPort 5274 `
  -ControlPlanePort 8116 `
  -MainWebPort 5273
~~~

## Management API

- `GET /configuration`：读取当前 Provider 列表及路径筛选；
- `PUT /configuration`：在 Control Plane 停止时保存完整运行配置；
- `POST /configuration/select-directory`：在 Management API 所在主机打开目录选择器，返回所选绝对路径；取消选择返回 `path: null`；
- `GET /services`：读取 AITools、Control Plane 和客户端生命周期的统一服务目录；
- `POST /services/{service_id}/start?epoch={epoch}`：启动单个服务；
- `POST /services/{service_id}/stop?epoch={epoch}`：停止单个服务；
- `POST /groups/core/start|stop`：启停核心服务组；
- `POST /groups/all/start|stop`：启停全部服务组；
- `GET /health`、`GET /ready`：Management API 探针。

运行配置默认持久化到 AITools 根目录的
`.data/aitools-service-manager/configuration.json`。旧版 version 1 单 Profile 配置会在首次加载时
原子迁移为 version 2 的 `providers[]`。新安装的 Control Plane 状态文件位于
`.data/multi-agent-v3/control-plane.jsonl`；如果只存在一个旧版
`control-plane-codex.jsonl` 或 `control-plane-fake.jsonl`，会继续使用该文件以保留历史；多个状态
文件同时存在时启动失败并要求人工收口。启动器日志和精确 PID/启动时间清单位于
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
