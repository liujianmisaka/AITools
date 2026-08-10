# AI Tools

多个相互独立的本地工具集合。每个工具都放在仓库根目录下的独立 Python 包中，并拥有自己的代码、测试和使用说明。所有工具可以共享根目录的 `.venv` 和 `requirements.txt`；仅在工具需要大型可选运行时时，允许在工具目录中提供额外依赖清单。

## 工具列表

| 工具 | 说明 |
| --- | --- |
| [codex_sessions](codex_sessions/README.md) | 使用 FastAPI 查询本机 Codex 会话名称和 ID |
| [multi-agent](multi-agent/README.md) | 使用 SDK 编排 Codex、Claude 和 GitHub Copilot 编码 Agent |
| [multi-agent-web](multi-agent-web/README.md) | 与编排核心解耦的 React + FastAPI Web 任务编排界面 |

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

在仓库根目录运行 `.\start-multi-agent-dev.ps1` 可同时启动并监督编排核心、独立 Web BFF 和 React 前端。Python 服务启用 reload，React 使用 Vite HMR。默认按 `Ctrl+C` 即可清理三个服务；使用 `-Detached` 可改为后台模式，再由 `.\stop-multi-agent-dev.ps1` 根据专用 PID 清单精确停止。

在 Windows Git Bash 中可使用 `./start-multi-agent-dev.sh` 和 `./stop-multi-agent-dev.sh`；后台模式参数为 `--detached`。PowerShell 与 Git Bash 使用各自独立的运行清单，启动和停止脚本应使用同一组。
