# Multi-Agent V3 MCP 网关

这是一个独立的 MCP stdio 适配器。它只调用 Control Plane HTTP API，不导入 V3
Runtime、Provider 或持久化包，因此不会改变核心组件架构。

协议层同时支持当前 2026-07-28 MCP stdio discovery 请求和旧版 initialize 握手，业务工具
保持同一套定义。

## 前置条件

1. Control Plane 已启动，并可通过 http://127.0.0.1:8016/ready 访问；
2. Control Plane 至少注册一个 Provider；
3. 每次调用 `delegate_task` 时传入目标绝对路径 `cwd`。

Control Plane 默认接受任意存在的绝对目录；如果统一服务平台配置了路径筛选，则只接受筛选
范围内的目录。Fake Profile 可使用 provider=fake、model=fake/model、effort=high 做无真实推理的
应用验收。

## 本地运行

使用 V3 已有 Python 环境，无需额外运行时依赖：

~~~powershell
$env:PYTHONPATH = "D:\dev\AITools\multi-agent-mcp\src"
$env:PYTHONUTF8 = "1"
D:\dev\AITools\multi-agent-v3\.venv\Scripts\python.exe -m misaka_mcp_gateway --control-plane-url http://127.0.0.1:8016
~~~

`--provider-id`、`--model` 和 `--effort` 现在是可选默认值。一次 `delegate_task` 调用中
显式传入的同名参数会覆盖默认值；如果调用参数与启动默认值都没有提供，网关会拒绝委派。
全部启动参数也支持对应的 MISAKA_* 环境变量，例如 MISAKA_MODEL、MISAKA_EFFORT 和
MISAKA_NETWORK_POLICY。有效组合可通过 `list_execution_options` 从 Control Plane 动态查询。

## Codex 配置

Codex 的 STDIO MCP 配置可以放在用户级 ~/.codex/config.toml，也可以放在受信任项目的
.codex/config.toml。示例：

~~~toml
[mcp_servers.multi_agent_v3]
command = "D:\\dev\\AITools\\multi-agent-v3\\.venv\\Scripts\\python.exe"
args = [
  "-m", "misaka_mcp_gateway",
  "--control-plane-url", "http://127.0.0.1:8016",
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

- list_execution_options：读取 Control Plane 当前注册的 Provider、模型及各模型支持的推理等级。
- delegate_task：必填参数为 `prompt` 和本次任务的绝对路径 `cwd`；`provider_id`、`model`、
  `effort` 可按调用选择，并覆盖 MCP 启动默认值；沙箱和网络策略仍由网关统一提供。
- get_task_status：读取一个委派的当前状态。
- list_tasks：读取当前 actor 可见的委派，可按状态过滤。
- cancel_task：请求取消一个委派。

网关不会绕过 Control Plane 的 actor 授权、路径筛选、Decision Gate 或恢复边界。`input.cwd`
和 `input.sandbox` 会被拒绝，工作目录只能通过工具顶层 `cwd` 提供。

## 验证

~~~powershell
D:\dev\AITools\multi-agent-v3\.venv\Scripts\pytest.exe -q
D:\dev\AITools\multi-agent-v3\.venv\Scripts\ruff.exe check .
D:\dev\AITools\multi-agent-v3\.venv\Scripts\basedpyright.exe -p pyproject.toml
~~~
