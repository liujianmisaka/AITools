# V3 A2A 独立运行方案

## 1. 定位

A2A 是一种协议和 Delegation Provider，不是整个核心内核，也不是唯一的 Agent-to-Agent 语义。

```text
Delegation Contract
    -> A2A Delegation Provider
        -> A2A HTTP/SSE Transport

A2A Node Profile
    -> 显式绑定以上模块和 Execution Runtime
```

A2A 必须可以在不启动 Control Plane、Workflow、Temporal、PostgreSQL 或 Web 的情况下独立运行。

## 2. 最小 Profile

```text
Composition Kernel
  + A2A Contract
  + Delegation Contract
  + A2A Task Runtime
  + Execution Runtime
  + A2A Delegation Provider
  + A2A HTTP/SSE Transport
  + Task Projection Store
```

Fake Agent、In-process Agent 或真实远程 Agent 都是可替换 Provider，不进入 A2A Contract。

## 3. 对象关系

```text
A2A Task
    -> Delegation Request
        -> Invocation Intent
            -> Execution
                -> Activation
                    -> Agent Provider
```

A2A Task、Delegation Run、Invocation Intent、Execution、Activation、Session 和 Provider Native ID 必须分别保存。

A2A Task Store 是协议投影或协议事实源，不能无说明地替代 Execution Fact Store。

A2A 的 `context_id`、`message_id` 和 Task ID 只能作为协议身份。平台必须把它们映射到通用的 Delegation、Session、Interaction Channel 和 Invocation 身份，不能把协议字段直接当作平台事实。

## 4. One-shot 和 Continuable

### One-shot

- 一个 Delegation Request；
- 一个可 dispose 的 Run；
- 一个结构化结果；
- 失败通过 stop reason 返回；
- Run dispose 必须等待资源完全停稳。

### Continuable

- 一个持久 Session；
- 至多一个 live Activation；
- 多个 FIFO follow-up；
- parent/child owner；
- delegation depth；
- 冷恢复和 Activation Reconciliation；
- child identity 和 report back。

Continuable A2A 还必须支持：

- follow-up message；
- reply / question；
- steer、pause、resume 和 cancel；
- message sequence、cursor 和 ack；
- accepted、delivered、processed、completed 的投递事实。

Continuable Delegation 不应通过不断创建独立 Task 来模拟。

## 5. 能力协商

A2A Agent Card 和 Delegation Provider Descriptor 必须发布：

- 支持的 Capability 和操作；
- structured output；
- streaming；
- cancellation；
- artifacts；
- session/resume；
- output schema；
- tool filter；
- persona；
- delegation depth；
- payload limits；
- reconciliation 限制。

请求能力不满足时，必须在创建 Activation 前拒绝。

## 6. 所有权和安全

每个 Task/Run/Execution 必须带 owner 和 scope。

```text
parent owner
    -> direct child owner
        -> child Activation
```

子 Agent 不能因为知道 Task ID 就获得控制权。取消、follow-up、report 和 list 都必须经过 owner/scope 校验。

A2A Transport 不承担授权事实；授权由 Delegation Runtime 和 Profile Policy 负责。

A2A Transport 也不承担父子关系、Decision Gate 或 Session Log 的事实所有权。

## 7. 事实和事件

A2A Stream 只承担协议实时事件，不作为平台终态事实。

```text
A2A Task Event       协议观察或协议状态
Execution Fact       平台执行事实
Session Event        Agent 上下文事实
Runtime Event        实时进度
Projection           UI/API 查询视图
```

这些事件必须定义映射方向和冲突优先级。

协议实时事件丢失后，客户端必须通过 Task/Channel cursor 重新读取已接受的 Message Fact 或 Durable Fact，而不是依赖重新启动一个新的 Task。

## 8. 独立验收

必须验证：

1. A2A Profile 可以独立启动；
2. Task、Delegation、Execution 和 Activation 身份分别可追踪；
3. One-shot 和 Continuable 语义分别成立；
4. 重复幂等键不会创建第二个 Execution 或 live write Activation；
5. 不支持的能力在 Activation 前拒绝；
6. owner/scope 错误无法读取或控制其他任务；
7. Provider 崩溃后不会盲目创建第二个写 Activation；
8. 断线重连使用事件序号或 Projection revision；
9. follow-up 不会创建意外的第二个 live write Activation；
10. owner/scope 和 Decision Gate 可以拒绝越权或未确认的副作用；
11. Server 停止会拒绝新请求并等待已接受工作；
12. 进程、端口、临时文件和测试任务全部清理。
