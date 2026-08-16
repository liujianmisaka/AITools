# 可信局域网威胁模型

## 范围与资产

受保护资产包括：

- 允许访问的源代码工作区和 Git worktree。
- 本机 Agent/CLI 会话及其 Provider 凭据。
- 工作流模板、实例、审批、事件和执行输出。
- Artifact Root 中的日志、补丁和附件。
- Temporal History 与 PostgreSQL Control DB。

## 信任边界

1. Web/BFF 是唯一局域网入口。
2. Control API、Temporal、PostgreSQL、Worker 和 Agent token 批量入口只接受回环连接。
3. 能访问 Web/BFF 的局域网用户视为同一信任级别；系统不验证用户身份。
4. 互联网、远程 Agent、外部反向代理和自动隧道均在范围外。

## 主要威胁与控制

| 威胁 | 控制 |
| --- | --- |
| DNS rebinding 或恶意网页调用本机服务 | Web/BFF 固定 Host/Origin allowlist；核心 API 不监听 LAN；正式运行同源且不启用 CORS |
| 任意路径读取或写入 | API 只接受 `workspace_id` 和 artifact key；服务端维护 root allowlist；拒绝绝对路径与目录逃逸 |
| Agent 获得过高工具权限 | 节点显式声明 `access_mode`；Provider Adapter 映射平台策略；写任务使用独立 worktree |
| 写任务至少一次执行导致重复副作用 | execution lease、Provider session ID、heartbeat、reconcile；不确定写状态进入人工处置 |
| 模板注入任意 Python/Shell | DSL 只允许注册节点类型、JMESPath 和版本化 schema；禁止 `eval` 和任意 callable |
| Provider 密钥泄露 | 平台不接收 Provider API Key；日志、异常和健康接口不返回连接字符串或底层错误消息 |
| Webhook 伪造与重放 | 来源网段、payload 上限、Inbox 去重；开放给其他 LAN 主机时使用 HMAC、时间戳和 nonce |
| Artifact 填满磁盘 | 配置单文件、总容量和保留期；原子写入；容量不足时任务失败而非虚假成功 |
| 后台服务或 Agent CLI 残留 | 启动脚本记录本轮 PID；有界停止；验收检查端口、进程和 worktree |

## 明确接受的风险

- 无登录意味着任一可信局域网用户都可以创建、运行、审批和取消任务。
- `operator_label` 只是未验证元数据，不提供不可抵赖审计。
- 恶意或已被攻陷的本机管理员可读取进程、文件和 Agent 配置；本项目不对抗本机管理员。
- 中间 token 是临时数据，浏览器或 BFF 重启时允许丢失；里程碑和最终输出不得丢失。

若未来需要互联网访问、访客网络访问或用户级权限，必须新增认证、授权和审计设计，不能沿用当前信任假设。
