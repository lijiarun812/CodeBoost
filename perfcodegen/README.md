# PerfCodeGen 轻量复现（base → correctness → time）

对 **PerfCodeGen（FORGE 2025 @ ICSE）** 的三相流程做单机复现，并接上统一的效率测量。
不依赖 ray / vllm / 多 CPU pod，**零第三方依赖**（装了 `psutil` 才会额外采内存）。

## 为什么不直接跑官方脚本

官方仓库的执行层是为集群设计的：`inference.py` 走批量推理接口、`evaluate_prompt.py` 要求
`task_num` 个 CPU pod 并行跑正确性与计时。本机跑不动，也不该为了课程作业去搭集群。
所以我们保留论文的**算法语义**与**提示词模板**，重写执行层：

| 复现了什么 | 怎么来的 |
|---|---|
| 三相流程（base → 纠正 → 性能） | 与 `evaluate_prompt.py` 的 phase/mode 一一对应 |
| 两类反馈（失败用例、最慢用例） | 直接用官方 `upstream/templates_chat.json` 里的 instruction |
| 代码抽取/清洗 | 直接调用官方 `upstream/sanitize.py`（Apache-2.0） |
| 效率指标 | `eval/metrics.py`，口径对齐官方 `evaluate_execution_time` |

## 环境

```bash
# 只用标准库就能跑；装 psutil 才会记录内存
pip install psutil   # 可选
```

## 三种用法

```bash
# 1) 离线自检（不调 API，验证管线与指标）
python perfcodegen/pcg_repro.py --mock --num 5 --samples 1

# 2) 真实模型（20 题 × 5 采样，约几分钟；带断点续跑）
python perfcodegen/pcg_repro.py --provider deepseek --model deepseek-chat \
    --num 20 --samples 5 --time-strategy feedback

# 3) 反馈粒度消融：simple（只让模型“优化”）vs feedback（给最慢用例）vs none（不优化）
python perfcodegen/pcg_repro.py --provider deepseek --model deepseek-chat \
    --num 20 --samples 1 --time-strategy simple
```

Windows 上建议显式指定解释器，例如 `C:\Python313\python.exe`。

## 指标

| 指标 | 定义 | 口径来源 |
|---|---|---|
| `%Correct` | 通过全部用例的采样比例 | 官方 |
| `Speedup` | `mean(gt_time / candidate_time)`，只统计正确采样 | 官方 |
| `%Opt` | 比 ground truth 快 10% 以上的题目比例 | 官方（阈值 0.1） |
| `Best@k` | 前 k 个采样里最快那次的 speedup / 是否 %Opt | 官方（k = 1,5,10） |

ground truth 的耗时用同一套测量核心测 `canonical_solution`，所以 speedup 是“同环境同口径”的比值。

## 与论文的差异（汇报第 4 部分要写）

1. **执行层**：单进程 + 子进程沙盒，串行采样；论文是集群并行。
2. **采样数**：默认 1（`--samples` 可调），论文 20；因此 `Best@5/10` 需要显式把采样数调上去才有意义。
3. **用例切分**：把 HumanEval `def check(candidate)` 里的每条 assert 当一条用例；论文用数据集自带
   testcase 输入，并会对大输入做放大。我们在 `--min-baseline-ms` 上做了补偿：基线太快的题直接跳过。
4. **时间测量**：重复执行放大到 `--target-time`（默认 0.25s）+ 多轮取最小值 + 报告 MAD；论文用单次 wall clock。
5. **采纳判据**：Pareto（时间至少快 5%，且内存不劣化 >5%），论文是“更优即采纳”。

## 成本与稳定性

- `--max-calls`：调用次数上限（默认 200），到达后保存已有结果并退出；
- `--resume`：按 `task_id` 跳过已完成题目（每题为单独 JSON，天然可断点）；
- API 失败自动退避重试 3 次（`urllib`，兼容任意 OpenAI 兼容接口）。

## 输出

```
results/perfcodegen/<model>/
├── HumanEval_31.json     # 逐题：base 采样、纠正轮、性能轮、最终代码与指标
└── summary.json          # %Correct / Speedup / %Opt / Best@k / 耗时 / 调用次数 / 跳过题
```

把 `summary.json` 交给 `eval/make_table.py`，就会生成“论文 vs 复现”的对照表。

## 已知问题（如实记录）

0. 第一次运行时 Python 会对 `upstream/sanitize.py` 打一条 `SyntaxWarning: invalid escape sequence '\s'`
   —— 那是**官方代码**里的正则字符串没写 raw string，不影响结果；我们不改上游文件（见 `upstream/NOTICE.md`）。
1. 亚毫秒题（如 HumanEval/55、63）的测量噪声与优化收益同量级，必须用 `--min-baseline-ms 1.0` 过滤，
   否则会重演中期汇报里“加速比在噪声里”的问题；
2. 单采样跑 `Best@5/10` 会退化成 `Best@1`，汇报时要注明采样数；
3. 只用 assert 计时，不覆盖“输入规模放大”这一手段，因此对 O(n²) 类改写的敏感度低于论文。

## 许可

`upstream/` 下的 4 个文件来自 PerfCodeGen 官方仓库（Apache-2.0，Copyright (c) 2023 Salesforce, Inc.），
仅用于复现对照；本目录其余代码为课程项目自写。
