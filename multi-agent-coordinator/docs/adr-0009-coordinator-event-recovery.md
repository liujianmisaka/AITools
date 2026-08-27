# ADR-0009：Coordinator 委托事件触发与恢复

- 状态：已接受
- 日期：2026-08-27

## 背景

委托可能持续数秒到数小时，Coordinator 不能依赖一次调用阻塞等待，也不能在进程重启或网络断开后
丢失被委托会话的输出。Multi-Agent V3 已通过历史事件接口和 SSE 暴露稳定的 session event sequence。

## 决策

1. `CoordinatorSession` 按 delegation 保存独立的 `ExecutionEventCursor`，游标表示下一个期望的 V3
   sequence；重复 sequence 幂等，跳跃 sequence 拒绝。
2. `CoordinatorEventBridge` 负责应用层事件适配：历史事件先重放，再从持久化游标订阅 SSE；断线后从
   当前游标重新读取历史并重连，直到收到 end envelope 或达到有界重连次数。
3. snapshot 只用于同步最新 V3 执行事实，event 映射为 `CoordinatorEvent`；Coordinator 不复制 V3
   payload 或 Provider 私有状态。
4. 终态、输出、等待输入和需要对账的事件标记 `activation_required`，应用层可调用
   `CoordinatorEventBridge.activate()` 开启一次新的有界 Coordinator activation。
5. 事件桥接不负责持久化。调用方必须在每次 update 后保存 `CoordinatorSession`，并在 activation 返回后
   保存新的 session/MAF AgentSession；恢复时从保存的 delegation cursor 继续。

## 结果

Coordinator 可以处理长时间委托、历史补偿、断线重连和重复事件，而不需要把 V3 事件存储或 SSE 细节
引入领域层。触发 activation 仍受 `CoordinatorAgentConfig.max_decision_steps` 限制，避免事件驱动形成
无界认知循环。
