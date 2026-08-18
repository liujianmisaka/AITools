# Multi-Agent V3

V3 是破坏性重构版本，核心是独立的 Python Composition Kernel 和可替换 Capability Provider。

当前实现顺序：

1. Kernel/Invocation Contracts；
2. Composition Kernel；
3. Invocation Runtime；
4. Agent、A2A 和基础能力；
5. Coordinators；
6. Application Profiles。

当前已完成：

- Kernel/Invocation Contracts；
- Composition Kernel；
- Invocation Runtime；
- Agent Capability、Fake Agent Provider 和 Codex SDK Provider；
- Policy、Artifact、Session、Process 和 Workspace 基础能力；
- 不依赖 Workflow、Temporal、Control Plane 或 Web 的 agent-host Fake Profile。

Codex Provider 使用独立的 `provider-codex` 包。真实调用必须显式提供模型、推理等级、
工作目录和沙箱类型；Provider 不读取默认模型，并要求服务端配置工作区白名单。模型目录
通过短生命周期 Codex SDK 客户端显式读取，不会在 `describe()` 中启动真实 API 调用。

## Codex 真实烟测

烟测脚本具有 Provider 启动、取消和整体执行截止时间，不会无限等待。必须显式传入模型、
effort、工作目录和实际使用的 Codex Home：

    uv run python -u examples/codex_smoke.py `
      --model pixel/gpt-5.6-luna `
      --effort high `
      --cwd D:/dev/AITools/multi-agent-v3 `
      --codex-home C:/Users/<user>/.codex `
      --timeout-seconds 60 `
      --rpc-timeout-seconds 20 `
      --allow-network `
      --ephemeral

`--allow-network` 会允许 Codex 进程联网，本烟测用它访问本地 OpenCodex 代理；它不等价于
“仅允许 loopback”。任务文件系统沙箱仍为 `read_only`。正式 Profile 必须显式提供 Codex
Home，并由平台网络策略实施 loopback/deny 边界，不能依赖进程默认用户目录。

V3 不导入 multi-agent-v2，不保留 V2 API、数据库模型或兼容层。

## 开发验证

在仓库根目录执行：

    $env:UV_CACHE_DIR = "D:/dev/AITools/.multi-agent-dev/uv-cache"
    uv sync --project multi-agent-v3 --all-packages
    uv run --project multi-agent-v3 --directory multi-agent-v3 pytest -q
    uv run --project multi-agent-v3 ruff check multi-agent-v3
    uv run --project multi-agent-v3 ruff format --check multi-agent-v3
    uv run --project multi-agent-v3 basedpyright -p multi-agent-v3/pyproject.toml
    uv run --project multi-agent-v3 python multi-agent-v3/tools/check_import_boundaries.py
