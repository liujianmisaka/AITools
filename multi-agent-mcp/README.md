# Multi-Agent V3 MCP 网关

这是一个独立的 MCP stdio 适配器。它只调用 Control Plane HTTP API，不导入 V3
Runtime、Provider 或持久化包，因此不会改变核心组件架构。

协议层同时支持当前 2026-07-28 MCP stdio discovery 请求和旧版 initialize 握手，业务工具
保持同一套定义。

## 前置条件

1. Control Plane 已启动，并可通过 http://127.0.0.1:8016/ready 访问；
2. workspace_id 已登记在 Control Plane 的工作区 allowlist 中；
3. 显式配置 Provider、模型和推理等级。

AITools 根目录的默认 Fake 启动脚本已经把 V3 项目目录登记为 workspace-1，可使用
provider=fake、model=fake/model、effort=high 做无真实推理的应用验收。

## 本地运行

使用 V3 已有 Python 环境，无需额外运行时依赖：

~~~powershell
$env:PYTHONPATH = "D:\dev\AITools\multi-agent-mcp\src"
$env:PYTHONUTF8 = "1"
D:\dev\AITools\multi-agent-v3\.venv\Scripts\python.exe -m misaka_mcp_gateway --control-plane-url http://127.0.0.1:8016 --workspace-id workspace-1 --provider-id codex --model <显式模型ID> --effort <显式推理等级>
~~~

全部参数也支持对应的 MISAKA_* 环境变量，例如 MISAKA_MODEL、MISAKA_EFFORT 和
MISAKA_NETWORK_POLICY。

## Codex 配置

Codex 的 STDIO MCP 配置可以放在用户级 ~/.codex/config.toml，也可以放在受信任项目的
.codex/config.toml。示例：

~~~toml
[mcp_servers.multi_agent_v3]
command = "D:\\dev\\AITools\\multi-agent-v3\\.venv\\Scripts\\python.exe"
args = [
  "-m", "misaka_mcp_gateway",
  "--control-plane-url", "http://127.0.0.1:8016",
  "--workspace-id", "workspace-1",
  "--provider-id", "codex",
  "--model", "<显式模型ID>",
  "--effort", "<显式推理等级>",
  "--sandbox", "workspace_write",
  "--network-policy", "deny",
]
startup_timeout_sec = 10
tool_timeout_sec = 120
required = true

[mcp_servers.multi_agent_v3.env]
PYTHONPATH = "D:\\dev\\AITools\\multi-agent-mcp\\src"
PYTHONUTF8 = "1"
~~~

也可以通过 CLI 添加同一 STDIO 命令。配置后使用 codex mcp list 或客户端中的 /mcp
确认工具已经连接。

## 工具

- delegate_task：必填参数只有 prompt；工作区、Provider、模型、推理等级、沙箱和网络策略由
  MCP 启动配置统一提供。
- get_task_status：读取一个委派的当前状态。
- list_tasks：读取当前 actor 可见的委派，可按状态过滤。
- cancel_task：请求取消一个委派。

网关不会绕过 Control Plane 的 actor 授权、工作区 allowlist、Decision Gate 或恢复边界。

## 验证

~~~powershell
D:\dev\AITools\multi-agent-v3\.venv\Scripts\pytest.exe -q
D:\dev\AITools\multi-agent-v3\.venv\Scripts\ruff.exe check .
D:\dev\AITools\multi-agent-v3\.venv\Scripts\basedpyright.exe -p pyproject.toml
~~~
