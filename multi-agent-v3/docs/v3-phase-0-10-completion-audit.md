# V3 Phase 0—10 完成审计

> 审计基线：2026-08-27
>
> 结论：最初 V3 改版计划的仓库内架构与代码目标已经收口。后续只剩依赖本机服务、凭据或
> PostgreSQL/Temporal 环境的运行验收，不再继续扩展通用核心。

## 1. 完成矩阵

| 阶段 | 状态 | 直接证据 |
|---|---|---|
| Phase 0 架构与边界冻结 | 完成 | `architecture-capability-first-v3.md`、`module-dependency-matrix-v3.md`、`implementation-map-v3.md` 和 ADR-0002 明确 Contract、Runtime、Provider、Coordinator、Profile、Transport 的依赖方向 |
| Phase 1 Contract Plane | 完成 | `interaction-contracts`、`delegation-contracts`、`invocation-contracts` 及对应 contract tests；`tools/check_import_boundaries.py` 验证基础契约不反向依赖基础设施 |
| Phase 2 Session / Interaction Persistence | 完成 | JSONL Session、Interaction、Delegation Store 支持重放、cursor、幂等、corruption 和 unsupported schema；PostgreSQL Adapter 保留独立实现 |
| Phase 3 Execution Continuation | 完成 | Invocation Runtime 将 prepare/start 分离，持久化 Provider Session Reference，使用 lease/epoch fencing，并覆盖取消、attach/reconcile、未知外部副作用和恢复 |
| Phase 4 Delegation Runtime | 完成 | `DelegationRuntime` 覆盖 one-shot、continuable、消息路由、question/reply、steer、pause/resume、cancel、reconcile、parent/child、fan-out 和报告回传 |
| Phase 5 Fake 纵向闭环 | 完成 | `test_fake_delegation_vertical.py` 覆盖两个 Fake 子 Agent、Decision Gate、消息、提问回复、报告、重启恢复和不重复启动 |
| Phase 6 Local Gateway | 完成 | Control Plane Delegation Port、HTTP/SSE API 与独立 `multi-agent-mcp` 网关提供 create/get/children/send/events/reply/cancel/reconcile/approve 和动态执行选项 |
| Phase 7 Codex Continuation | 完成 | Codex Provider 覆盖显式 model/effort、绝对 cwd、安全策略、Native Session、prepare/start、follow-up、steer、cancel、lease 冲突、对账和资源清理 |
| Phase 8 A2A Adapter | 完成 | A2A Provider、HTTP/SSE Transport 和远程客户端保持 Task/Execution/Delegation/Session 身份边界，并支持 cursor、取消和 persisted reconcile |
| Phase 9 Control Plane 与 UI | 完成 | Control Plane 只通过公开 Gateway/Profile Port；Web V3 提供委派列表、完整详情页、历史与实时 Agent 会话、消息、对账、Markdown/LaTeX 和服务状态 |
| Phase 10 稳定性与切换 | 完成 | Provider disposer/epoch、Session/Message 重建、Decision Gate、重启 fencing、无重复 Activation、共享 Provider Contract Test 和依赖边界均有测试 |

## 2. Phase 10 门禁逐项结论

1. Contract Plane 无基础设施依赖：由 `dependency-rules.toml` 和 import boundary 检查覆盖。
2. Provider 注册和移除可逆：Capability Catalog disposer 幂等，Invocation Runtime 移除后拒绝新工作但保留已接纳绑定。
3. Session Log 可重建：JSONL Session replay/checkpoint 与 reopen 测试通过。
4. Message cursor 可恢复：Interaction JSONL 重建、幂等和 Delegation cursor replay 测试通过。
5. Delegation parent/child 可审计：父子 scope、depth、attachment order、授权和 child report 均为持久事实。
6. Decision Gate 阻断副作用：未批准、旧 revision 和绕过 Runtime 的请求均在 Provider start 前失败。
7. 旧 owner/epoch 无法写新状态：Provider epoch mismatch、Session lease transfer 和 Service epoch fencing 均 fail closed。
8. Provider 崩溃不产生第二个 live write Activation：未知 start 进入 reconciliation；重复幂等请求和并发 follow-up 不重复启动。
9. 重启无重复 Agent、残留进程或孤儿 Session：Control Plane 对 live 状态做 fencing，Fake 纵向恢复不重新启动 Provider，Session Log 拒绝 orphan facts，Service Runtime 等待进程树清理。
10. Fake、Codex、A2A 共享同一 Contract Test：`tests/test_provider_contract.py` 以同一组参数化成功/取消用例验证公共 `InvocationProvider` 契约；Provider 扩展能力保留专属测试。
11. 旧 Workflow/Job 不再是核心依赖：仓库不存在 V2/legacy 运行时或双写兼容层；Workflow 位于独立可选 Coordinator/Profile，核心 Agent、Delegation、A2A 和 Managed Service 可独立运行。

现存 V3 `DurableJob`、`/jobs`、模板/实例及 Web 页面属于当前 Application Profile 产品能力，
不是旧核心兼容层。删除这些当前功能需要单独的产品迁移计划，不属于本次架构收口。

## 3. 本次最终门禁

- Coordinator：`91 passed`；Ruff、BasedPyright 通过。
- V3：`474 passed, 3 skipped`；Ruff、BasedPyright、import boundary 通过。
- Control Plane Session HTTP/SSE 与重启重放聚焦验收：`2 passed`。
- 新增共享 Provider Contract Test：Fake、Codex、A2A 共 `6 passed`。

三个 skip 都是显式外部环境门禁：

- `MULTI_AGENT_V3_POSTGRES_DSN`；
- `MULTI_AGENT_V3_TEMPORAL_TARGET`；
- 同时需要 PostgreSQL 与 Temporal 的 durable-agent 集成。

当前本机旧版 Control Plane 状态文件如果包含同一幂等键绑定多个 delegation，启动时会按
持久化契约拒绝恢复并报告 `DurableCorruption`。这是 fail-closed 的数据完整性门禁，不会
通过放宽幂等校验或自动丢弃历史事实来“修复”；真实运行验收应使用已迁移且一致的状态文件。

## 4. 收口后的工作边界

以下事项不再视为 V3 核心代码缺口：

- 本机 Codex/OpenCodex、Claude/OpenCodex 的在线连通性复验；
- PostgreSQL/Temporal 的真实环境复验；
- 删除当前 V3 Job/Template 产品页面；
- 新的 Coordinator 策略、更多 Provider、更多 UI 功能。

这些工作只有在真实部署环境、产品下线决定或新的功能目标明确后再启动，避免 V3 收口继续无边界扩张。
