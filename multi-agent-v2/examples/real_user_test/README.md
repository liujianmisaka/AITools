# 真实 Codex 用户测试

这个示例会通过独立 Agent Worker 启动一个真实 Codex CLI 实例，不使用 FakeRuntime。
Agent 在服务端创建的隔离 Git worktree 中读取 `input/left.txt` 和 `input/right.txt`，计算
加法并写入 `output/result.md`。主工作区不会被直接修改；有文件变更的 worktree 会被
保留，便于后续人工检查。

## 启动

在仓库根目录运行：

```powershell
.\start-multi-agent-v2-dev.ps1 -Detached
```

脚本会一次性启动 PostgreSQL、Temporal、执行迁移、启动全部 Core/Web/Worker 进程以及
Vite HMR。基础设施密码会随机生成并只保存到已忽略的
`.multi-agent-dev/v2/infrastructure-secrets.json`。

打开 `http://127.0.0.1:5174`，进入“工作流模板”，拖入当前目录的 `workflow.json`。
打开模板后选择“保存并运行”，输入：

```json
{
  "request_id": "manual-test-001"
}
```

成功结果应为：

```json
{
  "formula": "37 + 58 = 95",
  "result": 95,
  "report_path": "multi-agent-v2/examples/real_user_test/output/result.md",
  "request_id": "manual-test-001"
}
```

示例显式指定 `sensenova/deepseek-v4-flash` 和 `high`，不会使用 Codex 默认模型。导入前
请先在“模型目录”确认该模型与推理等级仍由当前 OpenCodex 配置提供。任务使用
`workspace_write` 和 `networkPolicy=agent_default`；它不宣称当前 Windows 部署具备完整
只读文件系统或网络隔离能力。

停止服务和本地基础设施：

```powershell
.\stop-multi-agent-v2-dev.ps1
```

停止操作保留 PostgreSQL named volume，后续启动仍可查看已经持久化的模板和实例。
