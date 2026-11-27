# CodeBoost

> 探究大模型生成代码的运行效率提升：**复现 EffiLearner**，并借鉴 **PerfCodeGen** 做进一步优化。

## 项目简介

在保持功能正确的前提下，降低大模型生成代码的运行时间与内存占用。
项目以 **EffiLearner** 为主线完成简化复现（把时间/内存剖析结果反馈给模型做自迭代优化），
并引入 **PerfCodeGen** 的执行反馈思路（用失败单测修正正确性、用最耗时单测改进性能）与
**EffiBench** 的效率指标来补强评测。

## 参考论文

- **PerfCodeGen**（FORGE 2025）：先用执行反馈修正功能，再用执行反馈改进运行效率。
- **EffiLearner**（NeurIPS 2024）：将时间/内存剖析反馈给模型进行优化（本项目复现主线）。
- **EffiBench**（NeurIPS 2024）：效率评测基准，1000 道效率关键题与 6 个效率指标。

## 当前进展

已完成 EffiLearner 的简化复现，在 4 个模型上跑通「100 题 × 3 轮自我优化」：

| 模型 | 成功执行 | 时间下降 | 内存下降（可测口径） |
|---|---|---|---|
| deepseek-flash | 87 / 100 | ↓ 27.5% | ↓ 26.4% |
| deepseek-v4-pro | 80 / 100 | ↓ 23.9% | ↓ 24.3% |
| glm-4.6 | 82 / 100 | ↓ 18.3% | ↓ 17.8% |
| qwen3-coder-flash | 79 / 100 | ↓ 15.6% | ↓ 17.6% |

> 结果详情见 `results/repro/` 与 `docs/实验记录.md`。**内存指标有两层修正**：
> `mem_mbs` 是 RSS-时间积分（≈平均常驻内存 × 执行时间），代码变快它自己就会掉；且约 30% 的题 final 积分为 0
> （采样点不足）。去掉时间因素并剔除零积分题后，可测口径是 **8.9% / 10.2% / 8.4% / 9.9%**，
> 详见 [`docs/表格_复现口径对比.md`](docs/表格_复现口径对比.md)；与论文的并排对照见
> [`docs/表格_论文vs复现.md`](docs/表格_论文vs复现.md)。

## 目录结构

```
CodeBoost/
├── README.md               # 本文件：项目总览
├── requirements.txt        # 依赖（复现 + 上游源码）
├── LICENSE
├── src/                    # 原版 EffiLearner 源码（上游复现基线）
│   ├── EffiLearner.py / gpt_EffiLearner.py
│   ├── gpt_generation.py / open_llm_generation.py
│   └── code_efficiency_calculator.py / ...
├── prompts.py              # 原版提示词
├── scripts/run_code.sh     # 原版运行脚本
├── effilearner/            # 我们的简化复现 + 优化入口
│   ├── effi_repro.py
│   └── README.md
├── perfcodegen/            # PerfCodeGen 三相复现（base → 纠正 → 性能）
│   ├── pcg_repro.py        # 驱动：零第三方依赖，支持 --mock
│   ├── README.md
│   └── upstream/           # 论文官方 sanitize / 提示词模板（Apache-2.0，见 NOTICE.md）
├── eval/                   # 统一测量与指标（论文口径）
│   ├── measure.py          # 逐 assert 计时 / 重复放大 / min-of-runs / MAD / 可选内存
│   ├── metrics.py          # %Correct / Speedup / %Opt / Best@k
│   ├── reanalyze_repro.py  # 旧口径 vs 归一化口径重算（零成本）
│   ├── make_table.py       # 论文 vs 复现对照表
│   └── paper_numbers.py    # 论文数值唯一数据源（带 PDF 页码）
├── datasets/               # sample_humaneval_20.json 入库；EffiBench 全量本地放置
├── results/repro/          # 复现结果 JSON（各模型 + comparison）
└── docs/
    ├── 分工与贡献说明.md
    ├── 优化计划.md         # 下一步优化任务与分工
    ├── 实验记录.md         # 实验配置与结果记录
    ├── 结果解读与冲分方案.md  # 两轮实验解读 + 评分表差距 + 个人任务卡 + 答辩 8 问
    ├── 论文数值_出处.md     # 论文数字的页码出处（答辩兜底）
    ├── 表格_论文vs复现.md
    ├── 表格_复现口径对比.md
    └── 上游README.md       # 原版仓库 README（留档）
```

### 原版源码 vs 简化复现

- `src/` + `prompts.py` + `scripts/`：EffiLearner **上游原版实现**（复现基线，保留以便对照与二次开发）。
- `effilearner/effi_repro.py`：我们重写的**轻量复现 + 优化入口**（纯 Python + psutil，跨平台，支持任意模型）。

## 快速开始

```bash
pip install -r requirements.txt
```

离线自检（不需要 Key）：

```bash
py effilearner/effi_repro.py --mock --num 3 --epochs 2
```

接入真实模型（PowerShell）：

```powershell
$env:EFFI_API_KEY="sk-..."
$env:EFFI_API_BASE="https://api.deepseek.com/v1"
$env:EFFI_MODEL="deepseek-flash"
py effilearner/effi_repro.py --num 100 --epochs 3
# 或用厂商预设一键切换：--provider deepseek --model deepseek-flash（--list-providers 查看全部）
```

> **数据集**：`datasets/dataset.json`（EffiBench，约 83MB）体积较大，未纳入版本库。
> 运行前请自备并放到 `datasets/dataset.json`，或用 `--dataset <路径>` 指定。
> 详细参数见 `effilearner/README.md`。

### PerfCodeGen 支线（无需任何 Key 即可自检）

```powershell
# 离线自检：验证三相流程与指标（不调 API）
py perfcodegen\pcg_repro.py --mock --num 5 --samples 1

# 真实模型：20 题 × 5 采样（HumanEval 小样已入库；默认用 --max-rel-mad 0.2 过滤测量不稳的题）
py perfcodegen\pcg_repro.py --provider deepseek --model deepseek-chat `
    --num 20 --samples 5 --time-strategy feedback
```

反馈粒度分 5 档（对应两条论文路线）：`none`（不优化）/ `simple`（只说优化）/ `feedback`（最慢用例）/
`effi3num`（EffiLearner 的 3 汇总数字档）/ `fusion`（最慢用例 + per-case 表 + 行级热点）。
证据包可用 `--evidence slowest|table|hotlines|all` 开关，早停用 `--min-gain/--patience`。
两篇论文的定位、8 维对比、5 个融合点与其他优化方向见
[`docs/方法对比与融合方案.md`](docs/方法对比与融合方案.md)，驱动细节见 [`perfcodegen/README.md`](perfcodegen/README.md)。

### 生成汇报用的两张表

```powershell
py eval\reanalyze_repro.py   # → docs/表格_复现口径对比.md（旧口径 vs 归一化口径）
py eval\make_table.py        # → docs/表格_论文vs复现.md（EffiLearner + PerfCodeGen）
```

## 实验结果（当前）

EffiBench 前 20 题（`--start 100`）、3 轮优化、论文判据（仅当 `mem_mbs` 更小才采纳）：

| 模型 | %Correct（原始） | %Correct（剔除不可执行） | %Opt | Speedup | ET 降幅 | MU 降幅 |
|---|---|---|---|---|---|---|
| deepseek-v4-pro | 85.0% | 100.0% | 58.8% | 1.090× | +8.0% | -0.1% |
| deepseek-flash | 75.0% | 100.0% | 66.7% | 1.218× | +16.4% | -0.1% |

复现命令（结果可完全重跑）：

```powershell
py effilearner\effi_repro.py --provider deepseek --model deepseek-v4-pro `
    --num 20 --start 100 --epochs 3 --accept-rule paper `
    --profile off --send-thinking yes --thinking disabled `
    --out results\matrix\deepseek-v4-pro

py eval\paper_tables.py   # → docs/论文对照与差异分析.md（论文三张表对照 + 差异原因）
```

**已知局限（答辩会问，先写清）**：

1. `MU`（论文 Table 1 的峰值内存列）在本批题上无信号：所有题 `peak_rss` 都贴着 20–21 MB 的解释器基线，与论文 80–91 MB 量级差 3 倍，需换大输入或更大规模的题；
2. `TMU`（本实现的 `mem_mbs = ∫RSS dt`）与时间高度共线（r = 0.94–0.97），**不能作为独立的「空间收益」证据**，只作内部对照；
3. 20 题里有 4 题的标准解本身跑不通（测试块与 `canonical_solution` 不匹配），任何模型都必然失败，因此 `%Correct` 需同时给两个口径；其中 `254`/`306` 是确定性缺陷（多批全挂），`267`/`312`/`314` 是偶发（换一批就过），故 `%Correct` 的分母会随批次抖动，跨批比较不能直接当模型能力差；
4. 早期版本曾出现「0% 采纳」，根因是逐行剖析不对称（基线不剖析、候选剖析，虚高 2.8×），已修复，证据见 `eval/timing_check.py`；
5. **无上一代 coder 模型对照臂**：账号权限限制下阿里云未开通 `qwen2.5-coder` 系列，本地亦无 Ollama；改用「轻量档（`deepseek-flash`）vs 三家主力」+ F0–F4 方法消融替代，用于区分「提升来自模型世代」还是「来自反馈方法」。这是已知局限，若需补齐，开通按量计费成本很低。

完整对照表与差异归因见 [`docs/论文对照与差异分析.md`](docs/论文对照与差异分析.md)。

## 小组成员与分工

| 成员 | 线路 | 角色 | 贡献率 |
|---|---|---|---|
| 李佳润（组长） | 复现线 | 论文协议复现、模型矩阵、论断审计、总集成、PPT 主讲 | 22% |
| 成员 A | 复现线 | 测量基础设施：harness、超时改造（心跳/降档）、输入阶梯、统计 | 20% |
| 成员 B | 优化线 | 数据集与难度分层（HumanEval/MBPP+/EffiBench/APPS）、结果聚合 | 20% |
| 成员 C | 优化线 | 反馈构造：行级时间/内存归因、热点区域反馈、F0–F4 消融 | 19% |
| 成员 D | 优化线 | Ours v2：时间+空间联合目标、Pareto 接受、实验调度 | 19% |

完整职责与答辩安排见 [`docs/分工与贡献说明.md`](docs/分工与贡献说明.md)。

## 下一步

期末按老师 6 条意见重做实验：180s 硬超时改心跳看门狗、反馈从「热点行」升级为「热点区域」、
Ours 增加空间维度、模型换成当前主力并保留旧模型对照臂、数据集扩到四层并做难度校准。
代码侧已在 `effilearner/effi_repro.py` 落地第一版（自适应超时、±N 行热点上下文、时间/空间加权评分、跳过易题）；
任务拆解见 [`docs/优化计划.md`](docs/优化计划.md)，人员细则见 [`docs/分工与贡献说明.md`](docs/分工与贡献说明.md)。
