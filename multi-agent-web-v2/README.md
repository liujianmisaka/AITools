# Multi-Agent Web V2

V2 的独立 React 控制台与 FastAPI Web/BFF。Web 工程不导入编排核心、Temporal
Workflow 或 Provider SDK，只通过回环 Control API 读取持久化投影和提交命令。

## 网络边界

- public listener：默认 `127.0.0.1:8021`，正式部署时是唯一允许绑定可信局域网的入口。
- internal stream listener：固定回环地址，默认 `127.0.0.1:8022`，仅接收 Agent Worker
  携带随机进程内 token 的有界 token batch。
- Control API：默认 `127.0.0.1:8011`，不向浏览器或局域网直接开放。
- PostgreSQL、Temporal、Worker 同样只允许回环或受控容器网络访问。
- 生产模式下 React 静态资源、HTTP API 和 SSE 同源，不启用宽泛 CORS。

本项目没有登录功能。能够访问 public listener 的局域网用户都能创建、运行、审批和
取消任务，因此只能部署在可信局域网，不可直接暴露到互联网。

## 开发启动

从仓库根目录运行：

```powershell
.\start-multi-agent-v2-dev.ps1
```

默认先通过 Docker Compose 启动 PostgreSQL-backed Temporal 和 PostgreSQL、执行 Alembic
迁移，再启动 Control API、两个 Worker、Dispatcher、Catalog Refresher、双监听 Web/BFF 和
Vite HMR。首次运行会在 `multi-agent-web-v2/frontend` 安装锁定的 npm 依赖，并在
`.multi-agent-dev/v2` 创建运行清单、日志和默认工作区配置。
随机生成的三个基础设施密码只保存在该忽略目录的 `infrastructure-secrets.json` 中。
默认写任务的 Git worktree 根目录位于仓库同级的
`.multi-agent-worktrees/<workspace-id>`，不会与已注册仓库根目录重叠。

常用选项：

```powershell
.\start-multi-agent-v2-dev.ps1 `
  -PublicHost 192.168.1.20 `
  -Detached

.\stop-multi-agent-v2-dev.ps1

# 使用外部已启动的 PostgreSQL/Temporal
.\start-multi-agent-v2-dev.ps1 -SkipInfrastructure

# 前台退出时保留本轮 Compose 基础设施
.\start-multi-agent-v2-dev.ps1 -KeepInfrastructure
```

Git Bash 使用同一套 PowerShell 生命周期监督逻辑：

```bash
./start-multi-agent-v2-dev.sh --public-host 192.168.1.20 --detached
./stop-multi-agent-v2-dev.sh
```

默认地址：

- HMR 前端：`http://127.0.0.1:5174`
- 同源 Web/BFF：`http://127.0.0.1:8021`
- Control API：`http://127.0.0.1:8011`，仅用于本机诊断

启动脚本会预检端口、有限等待健康状态并记录 PID 与启动时间。停止脚本只处理清单中且
启动时间匹配的进程树，不按进程名扫描。启动失败或前台运行收到 Ctrl+C 时也会回收已经
启动的进程。停止脚本默认关闭本轮管理的 Compose 容器和网络，但不会删除 PostgreSQL
named volume。缺少真实基础设施时，Worker 或 `/ready` 会明确失败，不会自动退回内存或
SQLite。

真实用户测试可在“工作流模板”页面拖入
`multi-agent-v2/examples/real_user_test/workflow.json`，再按示例 README 输入参数并运行。

## 生产运行

先构建前端：

```powershell
npm --prefix multi-agent-web-v2\frontend ci
npm --prefix multi-agent-web-v2\frontend run build
```

然后分别以受监督服务运行 V2 Core 组件和 Web/BFF：

```powershell
uv run --project multi-agent-v2 multi-agent-v2-api
uv run --project multi-agent-v2 multi-agent-v2-orchestration-worker
uv run --project multi-agent-v2 multi-agent-v2-agent-worker
uv run --project multi-agent-v2 multi-agent-v2-dispatcher
uv run --project multi-agent-v2 multi-agent-v2-catalog-refresher
uv run --project multi-agent-web-v2 multi-agent-web-v2
```

生产环境必须显式配置工作区 allowlist、允许的 Host/Origin、public listener 地址和内部
stream token。不要把 Control API 或 internal listener 绑定到局域网地址。

## SSE 与恢复

`GET /api/v2/stream` 合并两类消息：

- `milestone`：来自 PostgreSQL `workflow_events`，带单调 `id`，浏览器通过
  `Last-Event-ID` 重放；BFF 重启后仍可恢复。
- `token`：来自进程内 Stream Hub，不带 SSE `id`，有界且允许丢弃；BFF 重启后不重放，
  也不作为任务终态真相源。

页面重新进入实例详情时会先读取持久化投影，再连接 SSE；因此页面切换或浏览器短暂断线
不会丢失运行状态。

## 验证

```powershell
uv run --project multi-agent-web-v2 pytest
uv run --project multi-agent-web-v2 ruff check .
uv run --project multi-agent-web-v2 basedpyright
npm --prefix multi-agent-web-v2\frontend test -- --run
npm --prefix multi-agent-web-v2\frontend run build
```
