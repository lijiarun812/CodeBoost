# CodeBoost

> 探究大模型生成代码的运行效率提升：以 **PerfCodeGen** 为主线，借鉴 **EffiLearner** 的细粒度开销反馈。

## 项目简介

在保持功能正确的前提下，降低大模型生成代码的运行时间与内存占用。
项目以 **PerfCodeGen** 为研究主线：先用失败测试修正正确性，再用最耗时测试的执行反馈改进性能。
**EffiLearner** 的时间/内存剖析反馈作为增强反馈与消融路线；主实验题目优先从具备有效测试的
EffiBench 题目中筛选。筛题和评测规则见 [`experiment/PROTOCOL.md`](experiment/PROTOCOL.md)。

## 参考论文

- **PerfCodeGen**（FORGE 2025）：先用执行反馈修正功能，再用执行反馈改进运行效率。
- **EffiLearner**（NeurIPS 2024）：将时间/内存剖析反馈给模型进行优化（本项目复现主线）。
- **EffiBench**（NeurIPS 2024）：效率评测基准，1000 道效率关键题与 6 个效率指标。

## 当前进展

G0～G3 递进反馈实验已完成：G0 无反馈、G1 程序总耗时、G2 追加最慢测试、G3 再追加热点源码与执行次数。17 题、每组两次生成的结果见 [`experiment/results/g_report.md`](experiment/results/g_report.md)；各阶段预期和实测见同目录的 `g_stage1_feedback.md` 至 `g_stage4_findings.md`。当前结果未显示反馈越细效果越好，研究问题改为分析不同反馈颗粒度的收益与成本。复现入口与结果索引见 [`experiment/SETUP.md`](experiment/SETUP.md)。本轮只测执行时间，未得到可靠的内存比较。


已有 EffiLearner 简化复现，在 4 个模型上跑通「100 题 × 3 轮自我优化」。另有 12 题 HumanEval 反馈粒度预实验；其中不少题目过于简单，不能据此判断增强反馈的效果。

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

| 模型 | 厂商 | %Correct（原始） | %Correct（剔除不可执行） | %Opt（≥10% 快） | Speedup | ET 降幅 | MU 降幅 |
|---|---|---|---|---|---|---|---|
| glm-4.6 | 智谱 | 90.0% | 100.0% | 38.9% | 1.147× | +11.8% | +0.0% |
| deepseek-v4-pro | DeepSeek | 85.0% | 100.0% | 52.9% | 1.090× | +8.0% | -0.1% |
| qwen3-coder-flash | 阿里云 | 80.0% | 100.0% | 12.5% | 1.042× | +3.3% | +0.1% |
| deepseek-flash | DeepSeek（轻量档） | 75.0% | 100.0% | 60.0% | 1.218× | +16.4% | -0.1% |

复现命令（结果可完全重跑）：

```powershell
py effilearner\effi_repro.py --provider deepseek --model deepseek-v4-pro `
    --num 20 --start 100 --epochs 3 --accept-rule paper `
    --profile off --send-thinking yes --thinking disabled `
    --out results\matrix\deepseek-v4-pro

py eval\paper_tables.py   # → docs/论文对照与差异分析.md（论文三张表对照 + 差异原因）
```

### 空间可测性（升档实验，仅诊断）

原始规模下 MU 无信号；用**运行时构造**的大输入（不在源码里展开字面量，避免 compile 时间主导测量）升到 2×10⁵ 元素后：

| 档位 | 有效题 | 可分辨（`peak_rss − 基线 ≥ 5 MB`） | `peak_tm` 中位 |
|---|---|---|---|
| 原始规模 | 10 | 0/10（0%） | 0.0034 MB |
| 2×10⁵ 元素 | 10 | **6/10（60%）** | 2.96 MB（×870） |

- **不能对全数据集承诺 60%**，且 10 题里没升成功的要分两类说清楚：
  - **结构性不可升（2 题）**：`264`（标量参数，放大只改数值）、`270`（树节点参数，非字面量）；
  - **升档生效但信号不足（2 题）**：`267`（×11765，`delta` 2.7 MB）、`269`（×8334，`delta` 1.1 MB），差一点没到 5 MB 门槛——**不是方法失效**；
- 档位间 `peak_rss` 单点波动大（`254` 在 10⁶ 档反而比 2×10⁵ 小），对外只报**多档中位**；
- 空间结论固定用 **2×10⁵** 档：10⁶ 档部分题的时间测量会失效（`mean_trimmed_ms` 出现 0.0000）；
- 升档后耗时放大 100–2000 倍（1.4 ms → 360 ms），**任何时间数字都不得进入结论**，故只进 `diagnostic_only`。

### 与论文设置的差异（评分要求「说明和论文设置的差异」）

已对照原文核实：复现对象 **Effi-Learner**（arXiv 2405.15189）采用 **greedy 解码、单次生成**，评测集就是 **EffiBench**，方法与我们一致——这三点**不是差异**，不要写成「论文 20 采样 / 论文用 HumanEval」。

**A. 相对 Effi-Learner（复现对象）**

| # | 论文设置（出处） | 我们的设置 | 对结论的影响 |
|---|---|---|---|
| 1 | 6 个指标：ET / NET / MU / NMU / TMU / NTMU（§4.1、A.5） | 只实现 ET / MU / TMU，**缺 NET / NMU / NTMU** | 归一化对比做不了；绝对值只在同批题内可比 |
| 2 | 完整 EffiBench（1000 题） | 过滤后 120 题，本轮 20 题 | 样本量差一个数量级，只作方向性结论 |
| 3 | 剖析用 `line_profiler` + `memory_profiler`，且按**全部 open 用例合并**的每行总消耗报告（§3.2） | 按**单条用例**拆分（`split_cases`），逐用例计时 | 反馈粒度不同：论文给模型「整题热点」，我们给「最慢用例热点」；这是**有意的方法改动**，须在优化线里作为变量控制 |
| 4 | 有 **open / private** 两套测试集：open 用于自我优化的反馈与校验，private 用于最终评测（§4.1–4.2） | **只有一套**，反馈与评测同源 | **结果偏乐观**：模型能看到评测题，可能过拟合；答辩须主动声明 |
| 5 | 模型世代：GPT-4 / GPT-3.5 / Claude-3 / StarCoder2 | 2026 年主力（DeepSeek-V4 / GLM-4.6 / Qwen3-Coder） | 绝对值不可比，只比降幅方向 |

**B. 相对 PerfCodeGen（对照档，非复现对象）**

| # | 论文设置 | 我们的设置 | 说明 |
|---|---|---|---|
| 1 | 两相：先「正确性精炼」再「性能精炼」（§3） | 主线 Effi-Learner **本就无正确性精炼相**；两相只在 `perfcodegen/` 对照档实现 | 不是我们少做一相 |
| 2 | `Best@k`，`k ∈ {1,8,20}` | 单次生成 | PerfCodeGen 的采样设置，**与 Effi-Learner 无关** |
| 3 | `%Opt` = 比 ground truth **至少快 10%** | 借用该定义，早期误算为「更快即可」 | 已修正，两版数字不可混用 |

**指标口径（据原文 A.5，钉死不再猜）**：

- **`MU`** = 各测试用例中**最大内存消耗**再跨实例平均，单位 MB；论文 GPT-3.5 为 91.25 → 36.08 MB。
- **`TMU`** = 执行期内存曲线 `M(t)` 在总时长上的**数值积分**（面积），单位 MB·s。因此 `TMU = 平均内存 × ET`——**它与 ET 共线是定义使然，不是实现 bug**。
- 论文 `TMU ÷ ET ≈ 437 MB` 却大于自身 `MU = 91 MB`，说明 `M(t)` 的**基线（是否含解释器常驻）与统计范围（是否跨全部用例累加）论文未给出可复现细节**；故 `TMU` 只可比降幅，不可比绝对值。

**已知局限（答辩会问，先写清）**：

1. `MU`（论文 Table 1 的峰值内存列）在**原始规模**下无信号（所有题 `peak_rss` 贴着 20–21 MB 解释器基线）；升档到 2×10⁵ 后 6/10 题可分辨，见上表，但不是全数据集结论；
2. `TMU`（本实现的 `mem_mbs = ∫RSS dt`）与时间高度共线（r = 0.94–0.97）——按 A.5 定义 `TMU = 平均内存 × ET`，**共线由定义决定**，因此它**不能作为独立的「空间收益」证据**，只作内部对照；
3. 部分题的 `canonical_solution` 过不了 `small_test_cases`，任何模型都必然失败，因此 `%Correct` 同时给两个口径。三态分类（由 `eval/recompute.py` 判定）：
   - **`recomputed`（可补算，2 题）**：`250`、`285`——此前显示 `n/a` 不是数据集坏，而是 `new_metrics()` 直接调 harness 时没注入 `build_tree*` 前导；补前导后可正常测量；
   - **`excluded: dataset_defect`（5 题）**：`254`、`267`、`306`、`314`、`270`——canonical 加前导后仍 `AssertionError`；
   - **`retryable: final_code_missing`（2 题）**：`312`、`269`——canonical 能过，但记录里没存 `final_code`，需换模型/seed 重跑。
   另：`267`/`312`/`314` 在不同批次间时好时坏，故 `%Correct` 分母会随批次抖动，跨批比较不能直接当模型能力差；
4. `new_baseline` **无法重算**：模型生成的初始代码未落盘（旧记录只有 `final_code`）。已在新版 `effi_repro.py` 的每条记录中补存 `baseline_code`，但历史数据只能标 `not_recomputable`；
5. 早期版本曾出现「0% 采纳」，根因是逐行剖析不对称（基线不剖析、候选剖析，虚高 2.8×），已修复，证据见 `eval/timing_check.py`；
6. **无上一代 coder 模型对照臂**：账号权限限制下阿里云未开通 `qwen2.5-coder` 系列，本地亦无 Ollama；改用「轻量档（`deepseek-flash`）vs 三家主力」+ F0–F4 方法消融替代，用于区分「提升来自模型世代」还是「来自反馈方法」。这是已知局限，若需补齐，开通按量计费成本很低。

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
