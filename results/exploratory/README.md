# 两批真实模型探索性试跑（2026-11-28）

两个子目录的 JSON 均从当次 `outputs/` **逐字节复制**，没有重新生成或改写。
对应 SHA-256 列在下方；`experiments/frozen_pairs/preflight.py` 会核对这 7 个
文件、B 冻结快照、共享题单、输入档、提示词和逐次记录。B 题库归属见
`results/bank/README.md`。

## 五题 G0–G3、F0–F4

`gf5_2026-11-28/`：5 题，同一 t2 档、`test`、`args_hash`、题面与初始代码；
每档每题生成一次，**45 次请求**，44 次返回、1 次 SSL 调用失败且未重试。
9 档共用 `optimize.prompts.render_prompt`，各档反馈原文保存在预检文件。
G0–G3 保留各自标签和反馈语义，但共享框架、时间单位、初始代码及
G3 诊断工具与历史 G 实验不同；历史 G 结果不并入本批。

- `frozen5_shared_manifest_2026-11-28.json`：`82419f2f5867b373e6fe44ad3639adc1b5e022ba9f3ed081be9a3a1c58768f87`
- `frozen5_prompt_preflight_2026-11-28.json`：`54a052065b8687bf23470d7a60dc0e8a579cce73690c260a3f07fc25b6b953fe`
- `frozen5_gf_paired_trial_2026-11-28.json`：`75d1535b649ae5cba5ebb15092a48ce33f2ead3639023974bd75fcaa662c7714`

## 八题 M0/M1

`m0m1_2026-11-28/`：排除上述五题后，按共享题单中预定的 B scan 与
本地基线条件、题号顺序选 8 题；每题固定一个 t2 档。M0 给总耗时与
最慢用例并要求优化时间；M1 原样保留 M0，再增加匹配基线 Result 的
程序级 `peak_tracemalloc_mb`（MB）和空间约束。这是内存数字与目标措辞的
**组合改动**。每题每组生成两次，**32 次请求**，均返回，无自动重试。

- `m0m1_frozen8_manifest_2026-11-28.json`：`520368613083cfc8e2e8fbd788c020a402ccc85c8f294abe6dba50451d465470`
- `m0m1_frozen8_preflight_2026-11-28.json`：`924218b50672409443e323c948b75845d059cc389a3951b938aa97c109289aab`
- `m0m1_frozen8_trial_2026-11-28.json`：`ce05ade94e0faba83ceba19c52e2767f490eb9691edf41302f494559d0a8bc18`
- `m0m1_frozen8_summary_2026-11-28.json`：`3023cf1ba6af45360cce27e59049b1a22e144878c5b4447bf5a0b69acd1745ed`

两批均请求百炼 `qwen4-max`、temperature `0.7`、`enable_thinking=false`、
`max_tokens=2048`，未向模型发送 seed。返回模型 ID、原始提示词和反馈、
候选代码、错误样本及 token 用量逐次保存；客户端未提供费用或不可变修订号，
相应值为缺失。正确性、`mean_trimmed_ms` 时间与
`peak_tracemalloc_mb` 堆峰值均来自同题同档的共享 `eval.harness`；
插桩诊断不进入最终时间或空间指标。M0/M1 使用同一
`eval.space.pareto_accept()` 作探索性联合判定。运行解释器缺少 `psutil`，
`peak_rss_mb` 均缺失，RSS 门禁跳过；没有填造数值。

**两批都是探索性结果**：`formal_task=False`，正式 Result 写入数为 0。
五题与八题、以及历史 G 结果均不混算；少量生成与测量噪声不能证明
方法稳定胜出。
