# V2 初始容量与时延目标

以下数字是后续验收目标，不代表 Phase 1 已经达到。测试必须记录硬件、Provider 类型和工作区规模。

| 领域 | 初始目标 |
| --- | --- |
| 耐久等待 | 1,000 个同时等待 Timer、Signal 或 approval 的 Workflow，不占用 1,000 个 Worker slot |
| 本机 Agent 并行 | 默认上限 4，可配置至 8；超过上限进入 Temporal task queue |
| 非 Agent Activity | 单机并发 32，按 task queue 分离系统、Connector 和高权限 Git Activity |
| Control API | 20 个局域网浏览器客户端；普通投影查询 P95 小于 500 ms |
| 运行状态恢复 | 页面重新进入后 2 秒内显示当前投影；通过 `Last-Event-ID` 补齐持久化里程碑 |
| 健康检查 | 单组件默认 2 秒超时；`/ready` 总耗时不超过最慢组件超时加少量调度开销 |
| 启动验收 | 30 秒内给出成功或失败；失败必须有组件日志和非零退出码 |
| Workflow History | 接近配置阈值前 Continue-As-New；token 不写入 History |
| Artifact | 容量、单文件限制和保留期必须配置；默认值在 Artifact Store 实现阶段确定 |
| 恢复 | Control API、Worker、PostgreSQL 或 Temporal 分别重启后状态最终收敛，不产生不可见重复写任务 |

真实 Provider 容量测试必须使用显式模型和 effort，并与 Fake 压测分开报告。
