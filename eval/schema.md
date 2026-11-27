# eval/schema.md（v1 草案，待与组长 v0 合并冻结）

> 目的：全组所有实验的「一道题长什么样」和「一次实验记录长什么样」以本文件为唯一准绳。
> 任何字段变更必须升 `protocol_version`，并跑一遍 `python eval/harness.py --selftest` 与 12 题体检表。
>
> 状态：**v1 草案**（A 依据 `docs/优化计划.md` 第 8 节实现 harness 时固化，含实测字段）。
> 组长 v0 已落地但未推送；拿到后按「以 v0 的措辞为准、以 v1 的实测字段为准」合并。

---

## 一、Task（一道题）

```json
{
  "task_id": "EffiBench/3",
  "dataset": "humaneval | mbpp+ | effibench | apps",
  "kind": "humaneval | test_block",
  "entry_point": "solution",
  "task_name": "Longest Substring Without Repeating Characters",
  "code": "class Solution:\n    ...",
  "test": "solution = Solution()\nassert solution.lengthOfSubstring(...) == 3",
  "inputs": [
    {"tier": "t1", "n": null, "gen": "native", "args_hash": "sha256:..."}
  ],
  "difficulty": {
    "baseline_ms": 0.35,
    "headroom": 2.4,
    "cv": 0.04,
    "category": "string/hash"
  }
}
```

| 字段 | 含义 | 归属 |
|---|---|---|
| `task_id` | `数据集/题号` 唯一标识 | B |
| `dataset` | 四层之一 | B |
| `kind` | `humaneval`（用 `check(candidate)`）或 `test_block`（自包含测试块，EffiBench 用） | B |
| `entry_point` | 被测函数/类实例名（Humaneval 是函数名，EffiBench 是 `solution`） | B |
| `code` / `test` | 可直接 `exec` 的源码字符串 | B |
| `inputs[]` | 输入阶梯；`null`/`native` 表示不放大。**档位由 A 的 `eval/inputs.py` 生成**（native → gen200 → gen1000，EffiBench 用题目自带的 `test_case_generator` 放大用例数），B 负责把结果写进题库 | B |
| `difficulty` | **B 用 `calibrate_task()` 拿到数值后自己回填；A 不维护题库** | **B** |

## 二、Result（一次实验记录）

```json
{
  "run_id": "20261127-172710-HumanEval_31",
  "task_id": "HumanEval/31",
  "dataset": "humaneval",
  "method": "base | vanilla | f1 | f2 | f3 | ours",
  "started_at": "2026-11-27T17:27:10",
  "manifest": {
    "model": "deepseek-flash",
    "model_version": "deepseek-flash-0731",
    "temperature": 0.2,
    "seed": 42,
    "dataset_hash": "sha256:ab12cd34ef56gh78",
    "harness_commit": "ba9df05",
    "protocol_version": "v1"
  },
  "status": "ok",
  "error": null,
  "time": {
    "median_ms": 13.3311,
    "mean_trimmed_ms": 13.3311,
    "iqr": 0.62,
    "cv": 0.042,
    "raw_ms": [13.1, 13.4, 13.2],
    "tiers": [
      {"tier": "t1", "n": null, "gen": "native", "args_hash": "sha256:...",
       "status": "ok", "median_ms": 0.0124, "iqr": 0.0004, "cv": 0.025, "reps": 9832},
      {"tier": "t2", "n": 200, "gen": "seed:20261127", "args_hash": "sha256:...",
       "status": "ok", "median_ms": 17.3, "iqr": 0.9, "cv": 0.028, "reps": 14}
    ]
  },
  "mem": {
    "peak_tracemalloc_mb": 0.0031,
    "peak_rss_mb": 19.8
  },
  "speedup": 1.17,
  "mem_ratio": 0.95,
  "downgraded": false,
  "protocol_version": "v1",
  "code_source": "canonical | candidate"
}
```

| 字段 | 含义 | 归属 |
|---|---|---|
| `method` | `base` / `vanilla` / `f1`(总耗时) / `f2`(最慢用例) / `f3`(热点行) / `ours`(热点区域) | 全员 |
| `manifest` | **缺一项不进汇总**；`dataset_hash` 由 harness 对数据集文件算 sha256 前 16 位 | A 生成，复现线填 model |
| `status` | 五类，判定顺序见下 | A |
| `time` | 主口径 `mean_trimmed_ms`（E=12 去极值）；`median_ms` 同值保留兼容；`raw_ms` 必须留；`tiers[]` 记录每次升档尝试（含 `tier/n/gen/args_hash/status/reps`），**升档逻辑：t1 中位 < 2ms 或 t1 报 incorrect 且有后续档时自动升档** | A |
| `mem` | **双口径**：Python 分配峰值 + 进程 RSS 峰值；**禁止使用 mem_mbs 时间积分** | A |
| `speedup` / `mem_ratio` | 相对参照（GT 或 base）的比值，由汇总脚本填 | 复现线 / D |
| `downgraded` | 是否因超时被降档重试过 | A |

## 三、status 判定顺序（必须先判"跑没跑完"）

```
1. timeout            心跳 30s 中断 / 单档预算超时（含降档后仍超时）
2. resource_exceeded  进程 RSS 超过 --max-mem（默认 2048 MB）
3. api_failed         模型侧调用失败（重试后仍失败）—— 只出现在推理阶段
4. incorrect          代码能跑但测试不通过（含 SyntaxError、断言失败）
5. ok                 其他情况
```

> 铁律：`timeout` **不得**并入 `incorrect`。中期"跑得久=判错"的问题就出在这里。

## 四、目录与命名

```
results/<track>/<dataset>/<model>/<run_id>.json     # 单次实验
results/golden/golden_<timestamp>.json              # 12 题体检表
```

- `track`：`repro`（论文复现线）或 `opt`（优化线）
- 旧尺子（`legacy-psutil`）的历史结果留在 `results/repro/` 原地，**不与新数字进同一张表**

## 五、golden 三元组规则（体检表）

体检表**不锁死某个具体数字**，而是锁三件事：

1. `protocol_version` 必须是当前版本；
2. 12 题全部 `status=ok`；
3. 每题 `cv ≤ 10%`，且新值相对本次记录的基线浮动 ≤ 20%（同一台机器、同一天内）。

> 旧值 `HE/40 = 1.171x` 是**旧尺子**（3 次取中位数、逐 assert 计时）的产物，只能作对照列，不能当验收阈值。

## 六、冻结流程

1. A 提本文件 → 2. 组长 review（≤3 条修改）→ 3. 改完打 `protocol_version = v1` 冻结 → 4. B/C/D 只读引用，要改字段必须提 issue 并升版本号。

