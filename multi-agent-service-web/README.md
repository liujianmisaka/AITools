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
- `codex-app-server`：由 AITools Management API 直接托管，默认监听 `127.0.0.1:8048`，
  同时承载 Codex Provider 的结构化 SDK 控制连接与终端 Remote TUI 连接；
- `multi-agent-coordinator`：由 AITools Management API 直接托管，依赖 Control Plane，提供
  Coordinator HTTP API 和 `http://127.0.0.1:8020/mcp` Streamable HTTP MCP；
- `terminal-host`：由 AITools Management API 直接托管，依赖 Codex App Server，提供 Codex/Claude PTY；
- `web-v3`：由 AITools Management API 直接托管，启动时自动先启动 Control Plane 和 Terminal Host；
- `a2a-node`、`a2a-agent-host`：由 Control Plane 的静态服务目录托管，Management API
  通过 HTTP 代理启停和状态；
- `multi-agent-mcp`：由 Codex 或其他 MCP 客户端按需启动的 stdio 进程，只展示生命周期
  归属，不伪装成共享常驻服务。

页面只提交固定服务 ID、动作、当前 epoch 和结构化 Provider 配置，不接受任意命令、环境变量或
进程参数。配置覆盖只允许 Provider 选择、无凭据 endpoint 和凭据环境变量名等安全引用，不允许把
密钥、Header 或带凭据的 URL 持久化。
路径筛选只接受已存在的绝对目录，并由下一次启动的 Control Plane 强制执行。停止 Control
  Plane 前会先校验 epoch，再停止下游 A2A 服务、Coordinator 和主 Web，避免陈旧页面误停新一代进程。

服务组：

- `core`：Codex App Server、Control Plane、Coordinator、Terminal Host 与主 Web；
- `all`：核心服务与 Control Plane 发布的全部可控下游服务。

由于 Control Plane 是下游服务的依赖，停止 `core` 时也会先收口下游服务。

## 准备环境

首次运行需要准备 V3 Python 环境和两个前端的依赖：

~~~powershell
cd D:\dev\AITools\multi-agent-v3
uv sync --all-packages

cd ..\multi-agent-coordinator
uv sync --all-groups

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
`http://127.0.0.1:5174` 后，先在“运行配置与路径筛选”中添加一个或多个 Fake/Codex/Claude Provider，
填写各自的 Provider ID、对应运行目录、模型目录和网络隔离声明，再配置 Coordinator 模型、effort、
OpenAI-compatible Base URL、API Key 环境变量名和有界决策参数，最后配置可选的允许根路径。允许根路径
支持点击“选择文件夹”打开运行 Management API 的本机
目录对话框，也可以直接编辑文本；可重复选择多个目录。保存后再选择“启动核心”或“启动全部”。
允许根路径每行一个；
留空表示不筛选，MCP 可以为每次委派传入任意存在的绝对目录。

Claude Provider 使用 Claude Agent SDK。页面中的“Claude 运行后端”是当前 Control Plane 的全局
连接方式，必须明确选择“原生 Claude / Anthropic”或“OpenCodex 代理”，不能在同一个 Control Plane
内混用两套路由。

选择原生模式时，SDK 使用本机 Claude 配置和原生 `claude.exe`；Windows 上必须安装原生 Claude CLI
（npm 的 `claude.cmd` shim 不能直接由 SDK 启动），模型 ID 示例为 `claude-sonnet-4-5`。
选择 OpenCodex 模式时，在页面填写 Base URL（默认 `http://127.0.0.1:10100`）和令牌环境变量名
（默认 `ANTHROPIC_AUTH_TOKEN`），模型 ID 必须填写 OpenCodex 路由，例如 `AIXW/gpt-5.6-sol`。
使用默认本机 OpenCodex 网关时，平台会自动注入其标准令牌 `opencodex-proxy`，无需额外设置环境变量。
如果改用自定义网关地址或自定义令牌变量名，令牌本身不会写入运行配置；启动统一平台前，
应在启动它的宿主环境中设置对应变量，例如：

~~~powershell
$env:ANTHROPIC_AUTH_TOKEN = "opencodex-proxy"
$env:ANTHROPIC_BASE_URL = "http://127.0.0.1:10100"
~~~

Control Plane 启动时会按页面选择注入 OpenCodex 的模型发现、Host 管理和自动压缩环境；选择原生模式
时会清除这些 OpenCodex 路由变量。认证信息只通过 Claude 自身配置或进程环境提供，不写入运行配置。

Coordinator 默认使用 `pixel/gpt-5.6-luna`、`medium` effort 和
`http://127.0.0.1:10100/v1`。默认本机 OpenCodex 地址与 `OPENAI_API_KEY` 变量名组合在变量未设置时
使用固定本机令牌 `opencodex-proxy`；自定义地址、自定义变量名或官方 OpenAI 必须在启动统一平台前
提供对应环境变量。页面和配置文件只保存变量名，不保存密钥。`wait_timeout_ms=0` 表示 Coordinator
不占用长时间阻塞等待，而是在 V3 委派事件到达后触发下一次有界激活。

统一平台启动核心服务时顺序为
`Codex App Server -> Control Plane -> Coordinator -> Terminal Host -> Main Web`；停止时会先停止 Control
Plane 发布的下游服务，再停止 Coordinator、主 Web、Terminal Host、Control Plane，最后停止 Codex App
Server。Coordinator 会话状态
追加保存到 `.data/multi-agent-coordinator/sessions.jsonl`。

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
参数。Codex App Server、Control Plane、Coordinator 或 Terminal Host 运行期间配置为只读；需要修改时
先在统一平台停止核心服务。

同一统一实例内的 Codex Provider 共享一个 `codex_home` 和网络策略，但每次委派仍携带自己的
`provider_id`。平台在共享 App Server 中注册所有无凭据 endpoint 定义，再由 SDK 在创建/恢复线程时
提交对应的 `model_provider`，因此结构化事件和 `codex resume --remote` 观察的是同一个活动线程，不会
退回到彼此隔离的 stdio 子进程。配置多个 Codex Provider 时，每个 Provider 还必须声明互不重叠的
模型 ID 列表，用于把共享 App Server 的全局模型目录过滤成准确的 Provider 目录；只有一个 Codex
Provider 时可以留空并展示全部模型。

让 Codex 使用受管 Coordinator MCP：

~~~powershell
cd D:\dev\AITools
.\configure-multi-agent-coordinator-mcp.ps1
~~~

脚本会注册并回读验证 `multi_agent_coordinator -> http://127.0.0.1:8020/mcp`。调用前只需在
`http://127.0.0.1:5174` 启动核心服务；不需要再单独启动 Coordinator。直接调用底层 V3 工具时继续
使用 `configure-multi-agent-mcp.ps1` 配置的 `multi_agent_v3` stdio 网关。

启动 Control Plane 前，Management API 会在每个 Codex Provider 配置的 `codex_home` 中创建并立即
删除一个临时探测文件。该目录不仅保存配置和认证，也由 Codex App Server 写入 SQLite 运行状态；因此
启动 Management API 的宿主必须具有真实写权限。若管理面由另一个 Agent 的只读/工作区沙箱启动，
统一平台会在创建 Control Plane 进程前返回 `provider.codex_home_unwritable`，而不是把故障延迟到首次
委派。此时应从有权访问该目录的本机终端启动统一管理面，或在页面选择另一个已经完成认证且可写的
Codex Home；平台不会复制认证文件，也不会静默改写运行目录。

端口也可以独立覆盖：

~~~powershell
.\start-multi-agent-service-web.ps1 `
  -ManagementPort 8114 `
  -FrontendPort 5274 `
  -ControlPlanePort 8116 `
  -MainWebPort 5273 `
  -CoordinatorPort 8120 `
  -TerminalHostPort 8122 `
  -CodexAppServerPort 8148
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
`.data/aitools-service-manager/configuration.json`。旧版 version 1/2/3/4 配置会在首次加载时
原子迁移为 version 5 的 Provider、Claude 后端和 Coordinator 字段。新安装的 Control Plane 状态文件位于
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
