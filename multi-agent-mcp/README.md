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
  `effort` 可按调用选择，并覆盖 MCP 启动默认值；网关会为未指定 `channel_id` 的调用自动
  分配委托事件通道，便于管理页面实时观察；沙箱和网络策略仍由网关统一提供。
- get_task_status：读取一个委派的当前状态。
- list_tasks：读取当前 actor 可见的委派，可按状态过滤。
- cancel_task：请求取消一个委派。

`delegate_task` 的 `wait_timeout_ms` 默认为 0，即触发后立即返回；设置为正数时最多等待
指定毫秒数，完成则返回终态，超时则返回当前状态。允许范围是 0 到 300000 毫秒。
- wait_task：传入 `delegation_id` 和可选的 `timeout_ms`，对已经触发的任务进行一次有界等待。
  `timeout_ms` 默认为 0，表示立即读取一次状态；超时返回 `timed_out=true`、`terminal=false`
  和 `next_action="wait_task"`。需要取消时仍使用 `cancel_task`。`compact=true` 时会省略
  终态报告中的 `output`，只返回状态和错误/产物元数据。

网关不会绕过 Control Plane 的 actor 授权、路径筛选、Decision Gate 或恢复边界。`input.cwd`
和 `input.sandbox` 会被拒绝，工作目录只能通过工具顶层 `cwd` 提供。

## 触发式等待建议

长任务建议拆成两步，避免主会话在一次工具调用中持续等待：

~~~json
{
  "prompt": "分析当前项目并给出改进建议",
  "cwd": "D:/dev/project",
  "provider_id": "codex",
  "model": "gpt-5.6-sol",
  "effort": "high",
  "wait_timeout_ms": 0
}
~~~

网关会返回 `delegation_id`。之后按需调用：

~~~json
{
  "delegation_id": "delegation-...",
  "timeout_ms": 3000,
  "compact": true
}
~~~

`wait_timeout_ms`/`timeout_ms` 是委托业务等待时间；Codex 配置中的 `tool_timeout_sec`
仍是整个 MCP 工具调用的客户端硬上限，两者不要混用。网关不提供无限等待参数。

## 验证

~~~powershell
D:\dev\AITools\multi-agent-v3\.venv\Scripts\pytest.exe -q
D:\dev\AITools\multi-agent-v3\.venv\Scripts\ruff.exe check .
D:\dev\AITools\multi-agent-v3\.venv\Scripts\basedpyright.exe -p pyproject.toml
~~~
