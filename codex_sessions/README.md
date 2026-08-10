# Local Codex Sessions API

一个只读的 FastAPI 服务，用于查询本机全部 Codex 会话，并返回会话名称和 ID。本目录是独立工具边界，代码和测试不依赖仓库中的其它工具；Python 依赖由仓库根目录统一管理。

## 启动

在仓库根目录中执行：

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn codex_sessions.main:app --host 127.0.0.1 --port 8000
```

打开交互式接口文档：<http://127.0.0.1:8000/docs>

## 测试

```powershell
python -m unittest discover -s codex_sessions\tests -v
```

## 查询

```http
GET /sessions
```

默认同时返回活动会话和归档会话，并按最近更新时间倒序排列。为避免内部代理线程把完整任务正文作为标题返回，名称中的换行会转换为空格，超过 200 个字符的名称会以省略号截断：

```json
[
  {
    "name": "实现本地会话查询服务",
    "id": "019fd81f-95e6-7503-a7c6-202ef356882b"
  }
]
```

只查询未归档会话：

```http
GET /sessions?include_archived=false
```

## 配置

- `CODEX_HOME`：Codex 数据目录，默认是当前用户的 `~/.codex`。
- `CODEX_STATE_DB`：可选，直接指定 Codex 状态数据库路径，默认是 `$CODEX_HOME/state_5.sqlite`。

服务优先只读查询 `state_5.sqlite`，因为它包含活动和归档会话；如果 `session_index.jsonl` 中存在同一会话，则以其中最后一条 `thread_name` 作为显示名称。数据库不可用时降级为仅读取索引。Codex 本地数据库属于内部存储格式，未来版本若调整字段，需同步更新此工具。
