# V3 Invocation 生命周期

## 1. 对象关系

~~~text
Invocation
  -> zero or one live Activation
      -> one Provider Handle
          -> zero or more Runtime Events

Session
  -> zero or one live Activation
      -> FIFO message/input boundary
~~~

Invocation 是一次调用意图；Activation 是一次真实运行。Provider Session、A2A Task、HTTP Stream 和 Invocation 不得混为同一 ID。

## 2. 状态机

~~~mermaid
stateDiagram-v2
    [*] --> registered
    registered --> preflighting
    preflighting --> rejected
    preflighting --> resource_acquiring
    resource_acquiring --> prepared
    prepared --> starting
    starting --> running
    starting --> reconciliation_required
    running --> finalizing
    running --> stopping
    stopping --> finalizing
    finalizing --> succeeded
    finalizing --> failed
    finalizing --> cancelled
    finalizing --> reconciliation_required
    reconciliation_required --> reconciling
    reconciling --> succeeded
    reconciling --> failed
    reconciling --> cancelled
    reconciling --> reconciliation_required
    rejected --> [*]
    succeeded --> [*]
    failed --> [*]
    cancelled --> [*]
~~~

reconciliation_required 表示系统无法证明外部副作用是否已终止或提交，不能自动启动第二个写任务。

## 3. 请求边界

~~~python
class InvocationRequest:
    invocation_id: str
    capability_id: str
    operation: str
    input: JsonObject
    idempotency_key: str
    parent_invocation_id: str | None
    session_ref: SessionRef | None
    policy_context: PolicyContext
    completion_boundary: str
~~~

attempt 不属于逻辑请求指纹，只属于执行尝试和诊断记录。

## 4. 事件语义

### Durable Facts

~~~text
invocation/accepted
invocation/rejected
invocation/started
invocation/checkpoint
invocation/completed
invocation/failed
invocation/cancelled
invocation/reconciliation-required
~~~

### Runtime Events

~~~text
invocation/progress
agent/token
tool/output
resource/heartbeat
~~~

Runtime Event 可以丢失；Durable Fact 必须可恢复、可排序和可幂等重放。

## 5. 取消和释放

取消流程：

~~~text
close admission
  -> request provider stop
  -> await provider termination
  -> finalize invocation
  -> release resources
  -> publish terminal fact
~~~

只发送 interrupt、关闭 HTTP 连接或设置 cancelled 标志，都不能证明 Activation 已停止。

## 6. Session 规则

- 同一 provider 和 native session 默认最多一个 live Activation；
- Session 消息入队成功不等于 Invocation 完成；
- session_idle 不等于某一条消息的结果；
- Agent Provider 必须明确一次 Invocation 的完成边界；
- 崩溃恢复必须先 reconcile，不能盲目创建第二个 turn。

