# 两阶段加法任务示例

这个示例验证最小的任务依赖链：

1. `extract_formulas` 读取 `inputs/*.txt`，只输出来源文件和加法公式，不计算结果。
2. `calculate_results` 不再读取输入文件，只读取任务 1 通过 `{{tasks.extract_formulas.output}}` 注入的输出，计算并输出每条结果。

`workflow.json` 使用测试专用的 `addition_fake` Provider 和 `addition_example` 工作区，因此不会调用 Pi、Codex 或任何外部模型。端到端测试中的确定性 Fake Provider 会真实读取这些输入文件，并真实解析前一任务的 JSON 输出。

在仓库根目录运行：

```powershell
$env:PYTHONPATH = "$PWD\multi-agent"
.venv\Scripts\python.exe -B -m unittest discover -s multi-agent\tests -p test_addition_pipeline.py -v
```

预期公式：`12 + 30`、`7 + 8`、`100 + 23`；预期结果分别为 `42`、`15`、`123`。
