# AI Tools

多个相互独立的本地工具集合。每个工具都放在仓库根目录下的独立 Python 包中，并拥有自己的代码、测试和使用说明。所有工具可以共享根目录的 `.venv` 和 `requirements.txt`；仅在工具需要大型可选运行时时，允许在工具目录中提供额外依赖清单。

## 工具列表

| 工具 | 说明 |
| --- | --- |
| [codex_sessions](codex_sessions/README.md) | 使用 FastAPI 查询本机 Codex 会话名称和 ID |
| [multi-agent-v2](multi-agent-v2/README.md) | 基于 Temporal、PostgreSQL 和本地 Agent SDK 的持久化编排核心 |
| [multi-agent-web-v2](multi-agent-web-v2/README.md) | 与编排核心解耦的 React + FastAPI 局域网控制台 |
| [multi-agent-v3](multi-agent-v3/README.md) | Capability-First 的 V3 任务编排核心与 Control Plane |
| [multi-agent-mcp](multi-agent-mcp/README.md) | 通过 MCP 调用 V3 委派能力的独立 HTTP 网关 |
| [multi-agent-web-v3](multi-agent-web-v3/README.md) | V3 执行、委派、能力和决策可视化页面 |
| [multi-agent-service-web](multi-agent-service-web/README.md) | AITools 层独立的服务引导、生命周期管理 API 与页面 |

## 目录约定

```text
<tool_directory>/
  <import_package>/
    __init__.py
  tests/
  README.md
requirements.txt
```

当工具目录名本身就是合法包名时，`<tool_directory>` 与 `<import_package>` 可以合并为一层。各工具使用独立包名和独立测试目录，避免代码入口互相干扰；虚拟环境和通用第三方依赖在仓库根目录统一维护。

## Multi-Agent 开发环境

在仓库根目录运行 `.\start-multi-agent-v2-dev.ps1` 可一次性启动 PostgreSQL、Temporal、
执行数据库迁移，并监督 Control API、Worker、Dispatcher、Catalog Refresher、独立 Web/BFF
和 React 前端。Python 服务启用 reload，React 使用 Vite HMR。默认按 `Ctrl+C` 清理全部
服务；使用 `-Detached` 可改为后台模式，再由 `.\stop-multi-agent-v2-dev.ps1` 根据 PID
与启动时间清单精确停止。停止时会关闭本轮管理的 Compose 服务，但保留数据库 volume。

在 Windows Git Bash 中使用 `./start-multi-agent-v2-dev.sh` 和
`./stop-multi-agent-v2-dev.sh`；后台模式参数为 `--detached`。Git Bash 入口调用同一套
PowerShell 生命周期监督逻辑。

V2 默认用户入口为 `http://127.0.0.1:5174`。真实 Codex 用户测试可导入
[`multi-agent-v2/examples/real_user_test/workflow.json`](multi-agent-v2/examples/real_user_test/workflow.json)，
具体步骤见同目录 README。

V1 的 SQLite 运行时、旧 Web 控制台和兼容入口已在 V2 切换后删除，不存在双写路径。

## Multi-Agent V3 服务管理

V3 的统一引导入口是 AITools 层的 `multi-agent-service-web`。在仓库根目录运行：

~~~powershell
.\start-multi-agent-service-web.ps1
~~~

脚本只启动默认位于 `8014` 的 Management API 和 `5174` 的管理页面；随后可在页面中启动
Control Plane、主 Web 和下游 A2A 服务。Profile、Codex Home 和可选路径筛选也在该页面保存，
不再作为业务服务启动脚本参数。需要使用已保存配置一次启动管理面与核心服务时使用：

~~~powershell
.\start-multi-agent-v3-dev.ps1
~~~

`.\stop-multi-agent-v3-dev.ps1` 只停止核心及其下游、保留管理面；
`.\stop-multi-agent-service-web.ps1` 会停止全部受管服务和管理面。完整参数与架构边界见
[multi-agent-service-web/README.md](multi-agent-service-web/README.md)。
