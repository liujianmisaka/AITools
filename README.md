# AI Tools

多个相互独立的本地工具集合。每个工具都放在仓库根目录下的独立 Python 包中，并拥有自己的代码、测试和使用说明。所有工具可以共享根目录的 `.venv` 和 `requirements.txt`；仅在工具需要大型可选运行时时，允许在工具目录中提供额外依赖清单。

## 工具列表

| 工具 | 说明 |
| --- | --- |
| [codex_sessions](codex_sessions/README.md) | 使用 FastAPI 查询本机 Codex 会话名称和 ID |

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

