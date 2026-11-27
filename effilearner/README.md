# EffiLearner 简化复现

基于 **EffiLearner** 的「代码运行效率提升」简化复现，并借鉴 **PerfCodeGen** 的执行反馈思路。
仅依赖 OpenAI 兼容 API，纯 Python、跨平台（Windows / Linux），无需本地大模型与 CUDA。

## 原理

```
题目描述 ──► LLM 生成初始解 ──► 执行剖析(内存/时间/峰值)
      ──► 将开销(overhead)拼进提示 ──► LLM 自我优化 ──► 仅保留更优解(可多轮) ──► 汇总
```

- 内存指标：内存-时间积分 `mem_mbs`（MB·s）
- 时间指标：重复执行总墙钟时间 `exec_time`（s）
- 峰值指标：采样期间 RSS 峰值 `peak_mb`（MB）
- 仅当优化版可执行且 `mem_mbs` 更小时才保留（与原仓库判定一致）

## 目录结构

```
.
├── repro/
│   ├── effi_repro.py     # 复现主程序
│   └── README.md         # 本文件
├── datasets/
│   └── dataset.json      # 题目数据（EffiBench 前 N 道 class Solution 题）
└── results/
    └── repro/            # 运行结果 JSON
```

## 环境依赖

- Python 3.8+
- 依赖安装：

```bash
pip install requests psutil
```

## 快速开始

### 1. 离线自检（无需任何 Key）

```bash
py repro/effi_repro.py --mock --num 3 --epochs 2
```

`--mock` 用数据集的 `canonical_solution` 当初始代码、不调用 LLM，仅用于验证测量管线是否正常。

### 2. 接入真实模型

PowerShell（Windows）：

```powershell
$env:EFFI_API_KEY="sk-..."
$env:EFFI_API_BASE="https://api.deepseek.com/v1"
$env:EFFI_MODEL="deepseek-flash"
py repro/effi_repro.py --num 100 --epochs 3
```

或改用命令行参数：

```bash
py repro/effi_repro.py --num 100 --epochs 3 \
  --api-base https://api.deepseek.com/v1 \
  --api-key sk-... \
  --model deepseek-flash
```

> 思考型模型（如 DeepSeek V4）默认会先生成长段推理，既慢又可能返回空正文；
> 脚本默认 `--send-thinking auto`，仅对支持该字段的厂商发送 `thinking`，避免其他接口报 400。

### 3. 厂商预设 / 接入任意模型（推荐）

```bash
py repro/effi_repro.py --list-providers          # 查看内置预设
py repro/effi_repro.py --provider deepseek --model deepseek-flash --num 100 --epochs 3
py repro/effi_repro.py --provider zhipu    --model glm-4.6          --num 100 --epochs 3
```

内置预设：`deepseek / dashscope / zhipu / moonshot / openai / siliconflow / openrouter / ollama`。
接入**其他任意 OpenAI 兼容模型**时，直接用 `--api-base + --model + --api-key`，无需改代码：

```bash
py repro/effi_repro.py --api-base https://your-endpoint/v1 --model your-model --api-key sk-...
```

兼容性处理：脚本按厂商自动判断是否发送 `thinking` 字段；遇到不支持 `max_tokens`/`temperature`
的接口会自动回退（改用 `max_completion_tokens` 或去掉该字段），保证可接更多模型。

## 参数说明

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--dataset` | `datasets/dataset.json` | 题目数据文件 |
| `--out` | `results/repro` | 结果输出目录 |
| `--num` | `20` | 题目数量 |
| `--start` | `0` | 起始题目偏移 |
| `--epochs` | `3` | 自我优化轮数 |
| `--target-time` | `0.25` | 自动校准的目标单次测量时长（秒） |
| `--max-repeats` | `20000` | 重复次数上限（放大测量信号） |
| `--max-time` | `10` | 单次执行超时（秒） |
| `--max-tokens` | `4096` | 单次生成 token 预算 |
| `--thinking` | `disabled` | `enabled` / `disabled`（发送该字段时的取值） |
| `--provider` | 无 | 内置厂商预设（自动填 base/model 与 thinking 兼容性） |
| `--list-providers` | 关闭 | 列出全部内置预设 |
| `--send-thinking` | `auto` | `auto`/`yes`/`no`：是否发送 thinking 字段（auto 按厂商判断） |
| `--mock` | 关闭 | 不调 LLM 的离线自检 |
| `--api-base` / `--api-key` / `--model` | 取环境变量 | OpenAI 兼容接口配置 |
| `--compare` | 关闭 | 汇总 `results/repro` 下多个模型的结果 |

## 输出说明

每个题目记录 `baseline` 与 `final` 两组指标：

```json
{
  "problem_idx": 3,
  "task_name": "Longest Substring Without Repeating Characters",
  "status": "ok",
  "repeats": 20000,
  "baseline": { "exec_time": 0.0524, "mem_mbs": 1.164, "peak_mb": 22.24 },
  "final":    { "exec_time": 0.0478, "mem_mbs": 0.736, "peak_mb": 21.96 },
  "final_code": "class Solution: ...",
  "history": [ ... ]
}
```

结果文件名形如 `repro_<model>.json`，顶层含 `num_problems`、`num_ok`、`epochs` 等汇总字段。

## 当前复现结果（100 题 × 3 轮）

| 模型 | 成功执行 | 时间下降 | 内存下降（可测口径） |
|---|---|---|---|
| deepseek-v4-pro | 80 / 100 | ↓ 23.9% | ↓ 24.3% |
| deepseek-flash | 87 / 100 | ↓ 27.5% | ↓ 26.4% |
| qwen3-coder-flash | 79 / 100 | ↓ 15.6% | ↓ 17.6% |
| glm-4.6 | 82 / 100 | ↓ 18.3% | ↓ 17.8% |

## 多模型对比

```bash
py repro/effi_repro.py --compare
```

读取 `results/repro/` 下所有 `repro_*.json`，输出统一的对比表。

## 已知问题（如实记录）

1. **内存「假 100%」**：最终版经内存分配器预热后 RSS 不再增长，增量积分归零（100 题中约 28 题），需改用绝对 RSS / 归一化口径。
2. **失败率 13–21%**：主因是断言失败、语法错误、缺 `build_tree/decode_tree`、`ListNode/TreeNode` 等辅助函数。
3. **与论文不完全可比**：模型、数据规模、测量方式均不同，本仓库为「简化复现」。

## 参考

- EffiLearner: https://github.com/huangd1999/EffiLearner
- PerfCodeGen: arXiv:2412.03578
- EffiBench
