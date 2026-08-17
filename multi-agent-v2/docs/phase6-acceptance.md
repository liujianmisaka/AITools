# Phase 6 验收记录

本记录只列直接验证的运行证据，不以静态配置或 Fake 测试替代真实基础设施验收。

## 已验证

- Core 默认测试：166 passed，8 skipped；跳过项仅为显式 opt-in 的基础设施与容量测试。
- Web/BFF：14 passed。
- React：3 passed，生产构建成功。
- Ruff、BasedPyright、两个 `uv.lock` 均通过。
- 生成 Core Python、Web Python 和 Frontend 三份 CycloneDX SBOM。
- Secret scan 为 0；Git 视角下所有非忽略仓库文件中的 V1 运行路径为 0。
- 1000 个 Temporal 等待 Workflow 在仅 32 个 Workflow Task 并发槽位下全部启动、
  等待 Signal 并完成。
- PostgreSQL 16 的 7 项真实集成测试全部通过，包括 schema revision、namespace
  准入、幂等、Inbox、Outbox fencing 和投影不回退。
- PostgreSQL 使用持久化哨兵完成独立停止/启动，数据在重启后保持一致。
- Temporal dev service 使用持久化文件完成独立停止/启动，同一 Workflow ID 与 Run ID
  在重启后仍可查询，测试 Workflow 随后已终止。
- Control API 和 Web/BFF 分别停止并重新启动后，之前通过 BFF 创建的模板仍可读取。
- 实际局域网地址探针结果：public Web/BFF 返回 HTTP 200；Control API 和 internal
  stream listener 的局域网连接均超时；三者的监听配置分别为 public、loopback、
  loopback。
- 所有验收用 Core、BFF、Temporal、PostgreSQL 进程和测试端口均在结束后清理。

## PostgreSQL-backed Temporal 生产拓扑

- 验收日期：2026-08-17。
- Docker Desktop 4.87.0、Docker Engine 29.7.2、Docker Compose 5.4.0。
- 锁定镜像 `postgres:16.15`、`temporalio/server:1.31.2` 和
  `temporalio/admin-tools:1.31.2` 均已真实 pull。
- `docker compose config`、首次 `up` 和健康检查通过；Temporal schema 与 namespace
  一次性任务均以退出码 0 完成。
- Control DB 在真实 PostgreSQL 上从空库执行 Alembic `0001`、`0002`、`0003` 成功。
- `.\verify-multi-agent-v2.ps1 -RunInfrastructure -RunCapacity` 在该拓扑上完整通过：
  7 项 PostgreSQL/Temporal 集成测试和 1000 Workflow 容量测试均成功。
- `multi_agent_app` 无法连接 Temporal 数据库，`temporal_runtime` 无法连接 Control
  数据库，三角色隔离按预期生效。
- PostgreSQL 独立重启后，Control DB 哨兵保持一致；同一等待 Workflow 的 Workflow ID、
  Run ID 和 `RUNNING` 状态保持不变。
- Temporal 独立重启后，同一 Workflow Run 再次恢复为 `RUNNING`；测试 Workflow 随后
  已终止，数据库哨兵已删除。
- 验收结束后，Compose 容器、网络和本次新建的 PostgreSQL volume 均已删除；锁定镜像
  保留在本机缓存中。

## 结论

Phase 1–6 的实现、切换、安全边界、容量、真实依赖和 PostgreSQL-backed Temporal
重启收敛门槛已全部通过。
