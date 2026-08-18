# V3 A2A 独立运行方案

## 1. 目标

A2A 必须可以在不启动当前 Control Plane、Temporal、PostgreSQL 或 Web 的情况下独立运行。

最小 Profile：

~~~text
a2a-contracts
  + a2a-server
  + a2a-http
  + task-store-memory
  + fake-task-handler
~~~

## 2. 依赖关系

~~~mermaid
flowchart LR
    Client[A2A Client] --> HTTP[A2A HTTP Transport]
    HTTP --> Server[A2A Server]
    Server --> Handler[TaskHandler]
    Server --> Store[TaskStore]
    Handler --> Runtime[Invocation Runtime]
    Runtime --> Agent[Agent Provider]
    Runtime --> Artifact[Artifact Store]
~~~

A2A Contracts 不依赖 FastAPI。HTTP Transport 不依赖 Temporal。Server 不依赖 Codex。

## 3. 核心端口

~~~python
class TaskHandler(Protocol):
    async def submit(self, request: TaskRequest) -> TaskHandle: ...
    async def cancel(self, task_id: str, reason: str) -> None: ...

class TaskStore(Protocol):
    async def get(self, task_id: str) -> TaskSnapshot | None: ...
    async def put(self, snapshot: TaskSnapshot) -> None: ...

class TaskEventPublisher(Protocol):
    async def subscribe(self, task_id: str) -> AsyncIterator[TaskEvent]: ...
~~~

## 4. A2A 与 Invocation 的映射

~~~text
A2A Task submitted
    -> InvocationRequest created
    -> Activation started
    -> Invocation Events mapped to A2A Task Events
    -> Result/Artifact mapped to A2A response
~~~

A2A Task ID、Invocation ID、Activation ID 和 Provider Session ID 必须分别保存。

## 5. 能力协商

A2A Agent Card 必须发布：

- 支持的操作；
- structured output；
- streaming；
- cancellation；
- artifact；
- session/resume；
- input/output schema；
- payload limits。

请求能力不满足时，Server 在创建 Activation 前拒绝。

## 6. 独立验收

必须验证：

1. 只安装 A2A Profile 依赖即可启动；
2. 可以提交、查询、取消任务；
3. 可以订阅事件并处理断线重连；
4. 重复幂等键不会创建第二个 Invocation；
5. 不支持的能力返回稳定错误；
6. Server 停止时拒绝新请求并等待 Handler 释放；
7. 进程、端口、临时文件和测试任务全部清理。

