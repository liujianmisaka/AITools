# Multi-Agent Terminal Host

`multi-agent-terminal-host` 是 AITools 周边终端托管服务，不属于 V3 核心。它负责：

- 每个委派会话独立 PTY；
- ANSI/alternate-screen 终端状态、快照与有序增量；
- 多个只读观察者和单一输入控制租约；
- resize、detach、terminate 与浏览器重连；
- 允许工作目录校验、loopback 绑定、Origin 校验和本地令牌认证；
- 历史终端会话元数据的 JSONL 持久化。

Terminal Host 按委派记录中的 `provider_id` 选择已配置的 Provider，并连接原有会话：

- Codex：通过 `codex resume <session-id> --remote <app-server-url>` 打开 Remote TUI；
- Claude：通过 `claude --resume <session-id>` 打开交互会话；OpenCodex 模式由宿主显式注入网关环境，亦可配置 `ocx.ps1` 一类启动器。

创建终端会话不会启动新的委派，也不会改变 V3 的结构化生命周期；它只附着到已有的 `provider_session_id`。

V3 仍以 Provider 的结构化事件判定任务终态。终端输出只用于人工观察和交互，不能驱动委派状态机。

## 技术门禁结论

- Codex CLI 0.146.1：同一个 WebSocket App Server 可以同时服务结构化控制客户端和 `codex resume --remote` TUI。结构化客户端启动的活动 turn 能在 TUI 中实时显示，TUI 提交的新 turn 也会完整进入结构化事件流。因此 Codex 采用“持久 App Server + 结构化控制 + Remote TUI”。
- Claude Code 2.1.241：`agents --json --all` 可以枚举 `failed`、`blocked` 等状态，但 `ocx claude --bg` 没有把 OpenCodex 认证可靠继承到后台 supervisor，终态后的 `claude logs` 还依赖已退出的命名管道。因此不能把 `--bg + agents --json` 单独作为执行和证据通道；Claude 需要 Terminal Host 显式托管环境，并保留独立结构化生命周期证据。

## 本地开发

```powershell
npm install
npm run check
npm test
```

正常使用由 `multi-agent-service-web` 统一启动和停止，不需要手工运行本服务。
