# Multi-Agent V3

V3 是破坏性重构版本，核心是独立的 Python Composition Kernel 和可替换 Capability Provider。

当前实现顺序：

1. Kernel/Invocation Contracts；
2. Composition Kernel；
3. Invocation Runtime；
4. Agent、A2A 和基础能力；
5. Coordinators；
6. Application Profiles。

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
