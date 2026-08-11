# Multi-Agent Web

独立的 React + FastAPI Web 控制台，用于编排和观察 `multi-agent` 工作流。

## 解耦边界

- 本目录是仓库根级独立工具，Python 包名为 `multi_agent_web`。
- 不导入 `multi_agent`，不读取核心 SQLite，也不启动 Provider SDK。
- 只通过固定的 `MULTI_AGENT_CORE_URL` 调用核心 HTTP API，浏览器不能指定上游地址。
- 工作区只能从核心 `/api/v1/workspaces` 返回的 ID 中选择，前端不接受任意本机路径。
- DAG、Provider 能力、权限、审批、重试和工作流合法性仍由核心服务裁决。
- `frontend/` 是独立的 React/TypeScript/Vite 应用；`multi_agent_web/` 仅充当 HTTP BFF 和生产静态资源宿主。

## 前端架构

- React Router 提供工作流、运行、Provider 和设置的独立页面边界。
- Ant Design 提供稳定的 Layout、Modal、Drawer、Form、Table 等基础组件，应用框架与业务结构由本项目维护。
- React Flow 在中间内容区显示 DAG、依赖方向和执行状态。
- TanStack Query 只管理服务端数据；Zustand 只管理工作流草稿、节点选择和弹窗状态。

当前页面：

- `/templates/new`：工作流模板画布。任务通过 Modal 创建，选中节点后在右侧 Drawer 编辑参数。
- `/templates`：服务端持久化模板库，支持分页、打开和归档；未保存的新草稿会单独提示。
- `/templates/:templateId`：从核心加载指定模板版本，保存时使用乐观并发控制。
- `/instances`：持久化工作流实例列表；存在非终态实例时每秒刷新，页面重新进入时立即恢复。
- `/instances/:instanceId`：只读 DAG、节点状态流、输出、错误与 Provider 会话信息。
- `/providers`：核心服务发布的 Provider、模型类型、模型和推理等级。
- `/settings/workspaces`：只读展示服务端工作区白名单。

## 当前功能

- 查询 Provider 和工作区。
- 在弹窗创建任务，在右侧参数面板编辑、复制和删除任务。
- 在画布中构造和查看顺序、并行和汇合关系。
- 保存、重新加载和归档完整工作流模板；模板 ID、版本和脏状态由编辑器明确展示。
- “保存并创建实例”会先持久化当前模板版本，再创建独立工作流实例；历史实例始终使用不可变快照。
- 根据 Codex `config.toml` 指向的模型目录，依次选择任务级模型类型、`model` 和推理等级；不允许自由输入或使用默认模型补全。
- 展示 Provider 可用状态、模型数量、推理等级和工作区摘要，并支持刷新目录。
- 参数 Drawer 提供基础、模型、契约和高级四组设置，以及 Schema 格式化和严格模板。
- 编辑只读/写入权限、超时和输出 JSON Schema；Codex Schema 会在浏览器提交前检查严格对象约束，内置加法示例提供可直接执行的完整 Schema。
- 调用核心校验模板并创建工作流实例。
- 实例详情页轮询展示中文执行状态、DAG 进度、尝试次数、错误码与各任务输出，支持复制输出和取消。

## 模型目录来源

Web 前端不维护模型清单。核心服务按以下顺序解析 Codex 当前配置：

1. 读取 `MULTI_AGENT_CODEX_HOME/config.toml`。
2. 读取其中的 `model_provider`；未设置时遵循 Codex 的 `openai` 默认 Provider ID。
3. 读取 `model_catalog_json` 指向的目录文件。
4. 只发布 `visibility = "list"` 且 `supported_in_api = true` 的模型。
5. 使用模型 slug 的命名空间作为“模型类型”，使用 `supported_reasoning_levels` 生成推理等级下拉框。

当前 OpenCodex 配置的 `model_catalog_json` 指向 `opencodex-catalog.json`，因此页面会直接反映 OpenCodex 同步结果。核心按配置文件和目录文件的版本变化自动刷新；执行 `ocx sync` 后刷新网页即可重新获取模型列表，不需要重启 Multi-Agent 核心。每个 Codex 任务启动独立 SDK/CLI 客户端，并在任务结束或取消后关闭。

## 开发启动（推荐）

在仓库根目录执行：

```powershell
.\start-multi-agent-dev.ps1
```

首次使用先安装项目级前端依赖：

```powershell
Set-Location multi-agent-web\frontend
npm install
Set-Location ..\..
```

启动脚本会一次性启动并监督：

- 核心服务：`http://127.0.0.1:8010`
- Web BFF：`http://127.0.0.1:8020`
- React 前端页面：`http://127.0.0.1:5173`

启动成功后会立即显示地址、PID、工作区和日志目录。默认脚本保持在前台，每 30 秒输出一次运行心跳；按 `Ctrl+C` 会停止核心、Web BFF 和 React 前端。

两个 Uvicorn 服务都启用 `--reload`。React 前端由 Vite 提供 HMR；修改 TSX 或 CSS 后浏览器会直接更新，无需重启服务。

如果希望服务在后台运行并让启动命令立即返回：

```powershell
.\start-multi-agent-dev.ps1 -Detached
```

停止时执行：

```powershell
.\stop-multi-agent-dev.ps1
```

停止脚本会核对启动脚本记录的 PID、进程名、启动时间和可执行文件，再使用精确 PID 树停止服务；不会按名称扫描或停止其他 Codex、Python 进程。只有三个服务都确认停止后才会删除运行清单。PID 和日志保存在仓库根目录的 `.multi-agent-dev/`，该目录不会纳入版本控制。

可选参数示例：

```powershell
.\start-multi-agent-dev.ps1 `
  -CorePort 8010 `
  -WebPort 8020 `
  -WorkspaceId aitools `
  -WorkspacePath D:\dev\AITools `
  -HeartbeatSeconds 30
```

## 手动启动

先启动核心服务：

```powershell
$env:MULTI_AGENT_WORKSPACES = '{"aitools":"D:\\dev\\AITools"}'
$env:MULTI_AGENT_CODEX_BIN = 'C:\Users\liujian\scoop\shims\codex.exe'
$env:MULTI_AGENT_CODEX_HOME = 'C:\Users\liujian\.codex'
.venv\Scripts\python.exe -m uvicorn multi_agent.main:app --app-dir multi-agent --host 127.0.0.1 --port 8010 --reload
```

再启动 Web BFF：

```powershell
$env:MULTI_AGENT_CORE_URL = 'http://127.0.0.1:8010'
.venv\Scripts\python.exe -m uvicorn multi_agent_web.main:app --app-dir multi-agent-web --host 127.0.0.1 --port 8020 --reload
```

开发前端另开一个终端：

```powershell
$env:VITE_BFF_URL = 'http://127.0.0.1:8020'
npm --prefix multi-agent-web\frontend run dev -- --host 127.0.0.1 --port 5173 --strictPort
```

打开 `http://127.0.0.1:5173`。前端不会自动调用 Pi 或真实模型，只有点击“提交运行”后，核心服务才会按任务参数启动 Provider。

如需让 FastAPI 在 `8020` 直接托管生产资源，先执行：

```powershell
npm --prefix multi-agent-web\frontend run build
```

构建完成后访问 `http://127.0.0.1:8020` 会进入 React 应用。BFF 不包含备用静态页面；缺少构建时页面请求返回 `503`，API 仍可独立使用。

## Git Bash 启动

Windows 安装 Git for Windows 后，也可以在 Git Bash 中使用等价脚本：

```bash
# 推荐：前台监督模式，Ctrl+C 同时停止三个服务
./start-multi-agent-dev.sh

# 后台模式，健康检查通过后立即返回
./start-multi-agent-dev.sh --detached

# 停止由 Git Bash 脚本启动的服务
./stop-multi-agent-dev.sh
```

常用参数示例：

```bash
./start-multi-agent-dev.sh \
  --core-port 8010 \
  --web-port 8020 \
  --frontend-port 5173 \
  --workspace-id aitools \
  --workspace-path /d/dev/AITools \
  --heartbeat-seconds 30
```

Shell 脚本接受 Git Bash 路径（如 `/d/dev/AITools`）或 Windows 路径。它使用独立的 `.multi-agent-dev/processes.git-bash` 清单，不会覆盖 PowerShell 脚本的 `processes.json`。启动和停止脚本应成对使用。

## 测试

前端契约测试与构建：

```powershell
npm --prefix multi-agent-web\frontend test
npm --prefix multi-agent-web\frontend run build
```

Web BFF Fake 测试：

```powershell
$env:PYTHONPATH = "$PWD\multi-agent-web"
.venv\Scripts\python.exe -B -m unittest discover -s multi-agent-web\tests -t multi-agent-web -v
```

测试使用 HTTP MockTransport 和独立的 Fake Core，不导入多 Agent 核心代码，也不会调用真实 API。
