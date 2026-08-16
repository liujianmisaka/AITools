# Phase 6 验收记录

本记录区分“已经直接验证”的门槛和“仍依赖正式部署环境”的门槛，不以静态配置替代
真实运行证据。

## 已验证

- Core 默认测试：166 passed，8 skipped；跳过项仅为显式 opt-in 的基础设施与容量测试。
- Web/BFF：14 passed。
- React：3 passed，生产构建成功。
- Ruff、BasedPyright、两个 `uv.lock` 均通过。
- 生成 Core Python、Web Python 和 Frontend 三份 CycloneDX SBOM。
- Secret scan 为 0；Git 索引中 V1 运行路径为 0。
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

## 正式部署门槛

生产拓扑要求 Temporal Server 与 Control DB 都使用 PostgreSQL，并由
`deploy/local/compose.yaml` 提供三角色数据库隔离。当前 Windows 主机没有 Docker
daemon，因此本轮不能直接执行该 Compose 拓扑的 pull/up/restart 验收。

在具备 Docker Desktop 或等价容器运行时后，必须补跑：

```powershell
docker compose -f multi-agent-v2\deploy\local\compose.yaml config
docker compose -f multi-agent-v2\deploy\local\compose.yaml up -d
.\verify-multi-agent-v2.ps1 -RunInfrastructure -RunCapacity
docker compose -f multi-agent-v2\deploy\local\compose.yaml restart postgresql
docker compose -f multi-agent-v2\deploy\local\compose.yaml restart temporal
```

只有上述 PostgreSQL-backed Temporal 拓扑也完成重启收敛后，才可把生产替换门槛标记
为全部通过。
