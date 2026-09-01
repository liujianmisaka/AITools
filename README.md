# Multi-Agent V3

本仓库现在只维护 Multi-Agent V3。旧版编排核心、旧 Web、V2 开发环境和独立
Codex 会话查询工具均已移除，不提供兼容入口或双写路径。

V3 采用 Capability-First 架构：核心只负责能力、委派、策略、持久化和生命周期，
Coordinator、MCP、服务管理、终端托管与 Web 均作为清晰边界的外围组件存在。

## 项目组成

| 项目 | 职责 |
| --- | --- |
| [multi-agent-v3](multi-agent-v3/README.md) | V3 核心、Control Plane、Provider、A2A、持久化与能力运行时 |
| [multi-agent-mcp](multi-agent-mcp/README.md) | 面向 Codex 等 MCP 客户端的 V3 STDIO 网关 |
| [multi-agent-coordinator](multi-agent-coordinator/README.md) | 基于 Microsoft Agent Framework 的持续任务规划与委派服务 |
| [multi-agent-service-web](multi-agent-service-web/README.md) | AITools 层统一配置、服务启动、停止和状态管理 |
| [multi-agent-terminal-host](multi-agent-terminal-host/README.md) | Codex/Claude TUI 的 PTY 托管、重连和输入控制租约 |
| [multi-agent-web-v3](multi-agent-web-v3/README.md) | Coordinator、任务流、委派详情、实时会话与 TUI 页面 |

## 架构边界

```text
Codex / Claude / 外部系统
        │
        ├── multi_agent_coordinator MCP ── Coordinator
        │                                      │
        └── multi_agent_v3 MCP ────────────────┤
                                               ▼
                                      V3 Control Plane
                                               │
                              ┌────────────────┼───────────────┐
                              ▼                ▼               ▼
                         Codex Provider   Claude Provider   A2A Provider

Service Web ── 统一管理 Codex App Server、Control Plane、Coordinator、
                Terminal Host、主 Web 和下游 A2A 服务

Main Web ───── Coordinator API / Control Plane API / Terminal Host
```

- V3 核心不导入 Web、Terminal Host 或 Service Web。
- Coordinator 通过 V3 MCP/HTTP 接口进行结构化委派，不绕过 Control Plane。
- TUI 只用于显示和人工交互；V3 终态仍由 Provider 的结构化事件决定。
- 所有服务由统一平台启动，业务调用不依赖固定 WorkspaceRoot。

## 环境准备

需要 Python 3.12、`uv`、Node.js 24 或更高版本和 `npm`。真实使用
Codex/Claude Provider 时还需要安装对应 CLI；使用 OpenCodex 路由时应先启动本机
OpenCodex 服务。

首次安装依赖：

在仓库根目录执行：

```powershell
cd multi-agent-v3
uv sync --all-packages

cd ..\multi-agent-coordinator
uv sync --all-groups

cd ..\multi-agent-service-web
npm ci

cd ..\multi-agent-terminal-host
npm ci

cd ..\multi-agent-web-v3
npm ci
```

`multi-agent-mcp` 默认复用 `multi-agent-v3/.venv`，不需要单独维护 Python
虚拟环境。Coordinator 通过 editable dependency 使用该网关。

## 启动服务

正式使用以统一服务平台为入口。在仓库根目录运行：

```powershell
.\start-multi-agent-service-web.ps1
```

该命令只启动 Management API `http://127.0.0.1:8014` 和服务管理页面
`http://127.0.0.1:5174`。打开页面后：

1. 配置一个或多个 Codex、Claude 或 Fake Provider；
2. 配置 Coordinator 模型、effort 和端点；
3. 按需配置允许的工作目录根路径，留空表示允许传入任意存在的绝对路径；
4. 保存配置；
5. 点击“启动核心”或“启动全部”。

核心服务按以下顺序启动：

```text
Codex App Server
  -> Control Plane
  -> Coordinator
  -> Terminal Host
  -> Main Web
```

已经保存过运行配置时，也可以使用开发快捷入口一次启动管理面和核心服务：

```powershell
.\start-multi-agent-v3-dev.ps1
```

主 Web 默认地址为 `http://127.0.0.1:5173`。

## 停止服务

只停止核心及其下游、保留服务管理页面：

```powershell
.\stop-multi-agent-v3-dev.ps1
```

停止全部受管服务、Management API 和服务管理页面：

```powershell
.\stop-multi-agent-service-web.ps1
```

不要在服务运行时删除 `.tmp/multi-agent-service-web-runtime`，其中保存统一平台用于
识别和停止当前进程的运行清单。

## 配置 Codex MCP

直接调用 V3 委派能力：

```powershell
.\configure-multi-agent-mcp.ps1
```

脚本注册并回读验证用户级 `multi_agent_v3` STDIO MCP。它适合直接创建、查询、
继续、打断、对账和验收 V3 委派。

使用持续 Coordinator：

```powershell
.\configure-multi-agent-coordinator-mcp.ps1
```

脚本注册：

```text
multi_agent_coordinator -> http://127.0.0.1:8020/mcp
```

Coordinator 适合复杂目标的持续规划、并行委派、依赖推进和结果验收；
`multi_agent_v3` 是更底层的直接委派接口。两者可以同时配置。配置完成后需要
新建 Codex 会话，才能加载最新 MCP 配置。

## 真实使用流程

1. 打开 `http://127.0.0.1:5174` 并启动核心服务；
2. 在 Codex 中调用 `multi_agent_coordinator` 提交复杂目标，或调用
   `multi_agent_v3` 直接创建委派；
3. 打开 `http://127.0.0.1:5173`；
4. 从左侧选择 Coordinator 会话；
5. 在任务流中打开委派详情内部标签页；
6. 查看结构化 Agent 会话、历史输出和真实 Codex/Claude TUI；
7. 需要输入时点击“接管输入”；关闭页面只会断开观察，不会终止委派。

外部 Webhook、Git、Timer、Cron 或消息队列可以调用 Control Plane 的
`POST /delegations/trigger` 触发统一委派，不需要保持 MCP 会话阻塞等待。具体接口和
幂等规则见 [V3 事件触发委派说明](multi-agent-v3/README.md#事件触发委派会话)。

## 持久化数据

真实运行数据统一位于仓库根目录 `.data`：

```text
.data/
├── aitools-service-manager/      # Provider、Coordinator 和路径筛选配置
├── multi-agent-v3/               # Control Plane 委派与调用状态
├── multi-agent-coordinator/      # Coordinator 会话、事件和工具审计
├── multi-agent-terminal-host/    # TUI 会话索引与本机访问令牌
└── artifacts/                    # V3 Artifact 数据
```

`.data` 不是构建缓存。删除它会重置运行配置、委派状态、Coordinator 历史和
终端会话引用；执行清理、迁移或归档前必须先停止全部服务并备份所需数据。

## 测试

各项目保持独立测试边界，常用命令如下：

```powershell
cd multi-agent-v3
uv run pytest

cd ..\multi-agent-coordinator
uv run pytest

cd ..\multi-agent-mcp
..\multi-agent-v3\.venv\Scripts\python.exe -m pytest

cd ..\multi-agent-terminal-host
npm test

cd ..\multi-agent-web-v3
npm test
npm run build

cd ..\multi-agent-service-web
npm run build
```

更详细的配置、端口、网络策略和组件边界请查阅各项目 README。
