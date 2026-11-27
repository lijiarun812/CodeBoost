# 实验复现说明

本目录按时间保留三轮实验：12 题 HumanEval 管线预实验、EffiBench 首轮及第二轮探索、G0～G3 递进反馈实验。最新结论见 [`results/g_report.md`](results/g_report.md)，阶段记录见 [`results/g_stage1_feedback.md`](results/g_stage1_feedback.md) 至 [`results/g_stage4_findings.md`](results/g_stage4_findings.md)。各轮题集和口径不同，不能合并计算。

## 环境与数据

在 `D:\CodeBoost` 安装 `requirements.txt`，将 `.env.example` 复制为 `.env` 并在本机填写百炼密钥。`.env` 被 Git 忽略。G0～G3 使用 `qwen4-max`；保留免费额度用完即停，遇到 `AllocationQuota.FreeTierOnly` 会保存检查点并停止。

EffiBench 数据和公开 GPT-3.5 初始代码因体积未入库。执行本实验前，需要放置 `datasets/effibench.jsonl` 与 `datasets/effibench_gpt35.json`。已提交的 manifest、筛选记录、模型输出和分析结果可用于审阅现有实验；重新生成反馈或计时仍依赖原始数据和相同运行环境。

## G0～G3 流程

1. `python experiment/collect_feedback_g.py`：对上一轮筛出的 21 题采集并验证总耗时、最慢测试、热点；结果写入 `results/g_feedback_validation.json`，17 题通过。
2. `python experiment/run_feedback_g.py --preflight`：核对冻结题集和提示词递进关系，不调用模型。
3. `python experiment/run_feedback_g.py`：每题每组生成两次，逐次保存到 `results/g_results.json`。Windows 上可使用 `experiment/run_feedback_g.ps1`。
4. `python experiment/analyze_feedback_g.py`：对原测试及额外测试重复计时，更新 `results/g_analysis.json` 和 `results/g_report.md`。

G0 没有执行反馈；G1 加总耗时；G2 再加最慢测试及耗时；G3 再加热点行、执行次数及近似行耗时。四组共用初始代码、优化指令、模型与生成次数。额外测试来自同一基准生成器的不同随机种子，仍需真正独立的大输入复核。

## 结果索引

| 阶段 | 报告 | 主要原始记录 |
|---|---|---|
| HumanEval 管线预实验 | `results/report.md` | `results/results.json` |
| EffiBench 首轮 | `results/effibench_main_report.md` | `results/effibench_main.jsonl` |
| 额外测试与困难题筛选 | `results/stage1_validation.md`、`results/stage2_screening.md` | `results/effibench_holdout.json`、`results/gpt35_screen.jsonl` |
| EffiBench 第二轮 | `results/effibench_v2_report.md`、`results/stage3_generation.md`、`results/stage4_findings.md` | `results/effibench_v2_results.json` |
| G0～G3 | `results/g_report.md`、`results/g_stage1_feedback.md` 至 `results/g_stage4_findings.md` | `results/g_feedback_validation.json`、`results/g_results.json`、`results/g_analysis.json` |

代码会使用绝对路径计算仓库根目录，但计时受设备、Python 版本和系统负载影响。历史报告记录的是当时环境中的观测值，不保证在其他环境复现相同的毫秒数。
