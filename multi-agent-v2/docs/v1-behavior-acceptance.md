# V1 行为迁移验收清单

V2 不复用 V1 内部类型或数据库，但以下已存在的产品行为必须通过新的契约测试重新证明。

## 编排与持久化

- [ ] 模板与不可变模板版本分离。
- [ ] 模板实例启动后保存固定 plan hash。
- [ ] DAG 并行、join、失败传播和取消。
- [ ] 状态机 transition、循环上限和 Continue-As-New。
- [ ] 运行记录跨页面切换、API 重启和 Worker 重启后可恢复。

## Agent Runtime

- [ ] 每个 Agent 节点显式提供 Provider、模型和 effort，不存在默认模型路径。
- [ ] FakeRuntime 覆盖 new/resume、stream、cancel、steer、schema output 和 reconcile。
- [ ] CodexRuntime 从当前 OpenCodex/Codex runtime 读取模型目录。
- [ ] 同一会话串行，不同任务和 Provider 可以并行。
- [ ] `workspace_write` 不在状态不确定时自动启动第二次执行。

## 工作区与契约

- [ ] `workspace_id` 由服务端映射到 allowlist root。
- [ ] 只读任务与独立写 worktree 行为明确。
- [ ] 输入输出 JSON Schema 拒绝额外字段并验证 `additionalProperties: false`。
- [ ] Pi 只检查约定输入输出契约，不参与任务执行细节、Provider 选择或图推进。

## Trigger、Schedule 与事件

- [ ] Generic Webhook 进入 CloudEvents Inbox 并按 source/id 去重。
- [ ] Git 指定分支 commit 更新触发确定性实例。
- [ ] Cron、interval、calendar 和 one-time schedule 可创建、暂停、恢复和持久化。
- [ ] 内部 workflow/agent 事件使用版本化 CloudEvent 类型。
- [ ] 等待事件使用持久化 subscription，不依赖进程内注册。

## API、UI 与运维

- [ ] 工作流 JSON 打开/拖拽导入后持久化为模板版本。
- [ ] 模板与实例在 API 和 UI 中保持不同资源语义。
- [ ] `/live` 不依赖外部组件；`/ready` 对不可用依赖返回 503；组件错误不泄露连接信息。
- [ ] UI 通过投影和 SSE 恢复正在执行的实例。
- [ ] 启停脚本有总超时、逐组件日志、失败退出码和子进程清理。
- [ ] 默认 CI 只运行 Fake；真实 Agent 测试必须显式启用并清理 CLI、worktree 和服务进程。
