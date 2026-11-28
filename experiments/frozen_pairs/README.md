# 冻结题探索性试跑复现入口

从仓库根目录运行无模型预检：

```powershell
python -m experiments.frozen_pairs.preflight
python -m experiments.frozen_pairs.run_gf5
python -m experiments.frozen_pairs.run_m0m1
```

这三个默认入口只读检查 B 冻结快照、原始报告哈希、共享题单、
`task_id + tier + args_hash + test`、提示词和调用上限；不会导入
模型客户端或读取 `.env`。需要有支持本项目 `eval.harness` 的 Python 环境；
模型复跑另需仓库客户端的 `requests` 和本机百炼凭证。依赖与密钥均不入库。

**只有明确选择新输出路径并加 `--execute-model` 才发起模型调用**：

```powershell
python -m experiments.frozen_pairs.run_gf5 --execute-model --output <new-gf5-journal.json>
python -m experiments.frozen_pairs.run_m0m1 --execute-model --output <new-m0m1-journal.json>
```

运行器使用归档的共享题单和完整预检提示词，不重新选题，也不重测
基线来改写提示词。每次付费调用前记录意图、响应后立即落盘；重启只跳过
已完成的调用，`in_flight` 状态不自动重试。原始归档报告不可作为输出路径。
这两个命令是将来复跑的入口，本次整理只运行了无模型预检。

归档的 `frozen5_shared_manifest_2026-11-28.json` 与
`m0m1_frozen8_manifest_2026-11-28.json` 保存了当次选题规则和 45/32 次
上限；两份预检 JSON 保存完整提示词及 M0/M1 的提示词版本、阈值与
指标预注册。`frozen5_prompt_preflight_2026-11-28.json` 还记录了相对
历史 G 提示词的变化。候选都用同题同档 `eval.harness.measure_task()`
测量；M0/M1 的联合接受调用 `eval.space.pareto_accept()`。

## 与当次运行脚本的差异

当次脚本位于临时 `work/`，使用绝对 Windows 路径和交接 ZIP；
当次五题选题/预检还利用了临时筛查记录。现在的两个运行器从仓库内
**原字节归档**的题单与提示词读取输入，改用脚本位置推导仓库路径，
增加默认只读预检、显式付费开关和原始报告防覆盖。选题与提示词的
原始数据均保留，但这些整理后的脚本**不是**旧报告的生成版本，
也不声称重新生成了旧结果。新复跑会产生新的模型样本，不能覆盖或
混入当次报告。

当次运行脚本的原文件 SHA-256：

- `run_frozen5_paired.py`：`31c92c7b41a309f70ddea12079c1a003d95731594b93fbfe11a323923f006490`
- `run_m0m1_frozen8.py`：`1739296b759f4b94ca976d264738a3b3568fd65a1fc5d95a5b009b297aeeceab`

运行脚本本身不写正式 `method=ours` Result；批次保持
`formal_task=False`，记录为 exploratory/diagnostic。
