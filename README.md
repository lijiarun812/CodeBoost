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

> 结果详情见 `results/repro/` 与 `docs/实验记录.md`；内存指标存在已知“假 100%”问题，见下文与优化计划。

## 目录结构

```
CodeBoost/
├── README.md               # 本文件：项目总览
├── requirements.txt        # 依赖
├── effilearner/            # EffiLearner 路线：简化复现代码
│   ├── effi_repro.py
│   └── README.md           # 复现说明（环境/参数/结果）
├── perfcodegen/            # PerfCodeGen 路线（待建设）
├── eval/                   # 效率评测脚本（待建设）
├── results/
│   └── repro/              # 复现结果 JSON（各模型 + comparison）
└── docs/
    ├── 分工与贡献说明.md
    ├── 优化计划.md         # 下一步优化任务与分工
    └── 实验记录.md         # 实验配置与结果记录
```

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
```

> **数据集**：`datasets/dataset.json`（EffiBench，约 87MB）体积较大，未纳入版本库。
> 运行前请自备并放到 `datasets/dataset.json`，或用 `--dataset <路径>` 指定。
> 详细参数见 `effilearner/README.md`。

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
任务拆解见 [`docs/优化计划.md`](docs/优化计划.md)，人员细则见 [`docs/分工与贡献说明.md`](docs/分工与贡献说明.md)。
