# Phase 7 验收记录

> 日期：2026-08-17
> 分支：`feature/multi-agent-v2`
> 范围：V2.1 本机运行时加固、执行证据和多分支示例
> 结论：代码与本机基础设施验收通过；禁止 push

## 1. 交付范围

- 受管子进程、敏感环境清理、有界输出和 Windows Job Object；
- 沙箱实际 enforcement 证明与 fail-closed 准入；
- Agent 执行证据、Artifact 元数据、Alembic `0004`；
- Artifact 原子写入、目录同步、SHA-256 读取校验和相对路径约束；
- 平台工具 Guard/Approval/Schema 流水线和运行时不变量；
- 人工问题批次、Temporal 耐久命令、过期准入；
- `CredentialRef`、环境变量/原子 JSON 凭据和 Webhook 动态轮换；
- 动态子代理身份、谱系、能力与资源预算；
- 确定性 Event Catalog 和 freshness 守卫；
- 安装后 console script 验收和可运行的多分支 Temporal 示例。

## 2. 本机基础设施

验收使用隔离 Compose 项目名 `multi-agent-v2-phase7`：

| 组件 | 验收版本 |
| --- | --- |
| Docker Engine | 29.7.2 |
| Docker Compose | 5.4.0 |
| PostgreSQL | 16.15 |
| Temporal Server | 1.31.2 |
| Temporal Admin Tools | 1.31.2 |

实际完成：

- PostgreSQL 和 Temporal 健康；
- Temporal schema 与 namespace 初始化任务退出码为 0；
- 空数据库依次执行 `0001 -> 0002 -> 0003 -> 0004`；
- `0004 -> 0003 -> 0004` downgrade/upgrade 往返通过；
- 真实 PostgreSQL/Temporal 集成测试 8 项通过；
- 1000 个等待 Workflow 容量测试通过。

验收后执行 `docker compose down --volumes --remove-orphans`。隔离容器、网络和
volume 已删除，`5432` 与 `7233` 未留下监听进程。

## 3. 安装产物

`tools/phase7_acceptance.py` 使用硬超时完成以下检查：

1. 构建 wheel；
2. 安装 wheel；
3. 检查 7 个 console script；
4. 运行 Event Catalog 与 Preflight；
5. 让 API、Agent Worker、Orchestration Worker、Dispatcher 和 Catalog Refresher
   在错误配置下快速、带诊断地非零退出。

入口集合：

```text
multi-agent-v2-agent-worker
multi-agent-v2-api
multi-agent-v2-catalog-refresher
multi-agent-v2-dispatcher
multi-agent-v2-event-catalog
multi-agent-v2-orchestration-worker
multi-agent-v2-preflight
```

## 4. 多分支执行流

[`../examples/multi_branch/workflow.json`](../examples/multi_branch/workflow.json) 覆盖：

```text
prepare
  +-- agent_check
  +-- policy_check
          |
      checks_joined
          |
        route
       /     \
    ship    review
```

测试分别传入 `release=true` 和 `release=false`，证明两条检查分支并发、两个终态
互斥，并 replay 两条真实 Temporal History。示例只使用 Fake Agent，不产生真实
模型调用。

## 5. 质量门禁

最终门禁包含：

```powershell
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run basedpyright
git diff --check
```

提交前完整门禁结果为 `218 passed, 9 skipped`。9 个默认 skip 由 8 个基础设施测试
和 1 个容量测试组成；它们已通过显式开关单独真实运行，不代表验收缺失。

## 6. 事实源与安全结论

- Temporal 仍是 Workflow/Node 状态的唯一持久化事实源；
- PostgreSQL 执行证据只保存已观察事实，不反向推进 Workflow；
- execution event 使用确定性事件 ID，使 Activity 重试不会追加第二个相同事实；
- 不确定的 workspace-write 执行进入 `reconciliation_required` 并保留 worktree；
- 已确定终态的干净、未变更 worktree 才允许自动删除；
- Provider API Key 不由平台读取、保存或轮换；
- 凭据值不进入模板、API 响应、Temporal input、数据库事件、日志或 Artifact 元数据。
