# eval 数据契约（Task / Result）

> 状态：**v0 草案**（2026-11-27 组长起草，待成员 A review 后升 v1 冻结）
> 冻结后改动规则：升 `protocol_version` → 迁移全部调用方 → 重跑 golden test，缺一不可。

## 1. 文件与命名

```
results/<track>/<dataset>/<model>/<run_id>.json
```

| 段 | 取值 | 谁定 |
|---|---|---|
| `track` | `effilearner` / `perfcodegen` / `optimize` | 固定 |
| `dataset` | `humaneval` / `mbpp+` / `effibench` / `apps` | 固定 |
| `model` | 小写 slug，非字母数字一律换 `_`（如 `deepseek_v4_pro`） | 组长 |
| `run_id` | `<YYYYMMDDTHHMMSS>_<model_slug>_n<题数>_s<seed>_<commit>` | 调用方 |

`manifest.harness_commit` 取 `git rev-parse --short HEAD`；工作区脏时追加 `-dirty`；不在 git 仓库时写 `nogit-<harness.py 文件哈希前 8 位>`。

## 2. Task

| 字段 | 类型 | 必填 | 谁填 | 说明 |
|---|---|---|---|---|
| `task_id` | string | ✅ | B | 全局唯一，如 `HumanEval/40`、`effibench/137` |
| `dataset` | enum | ✅ | B | 见上表 |
| `entry_point` | string | ✅ | B | 被测函数名 |
| `prompt` | string | ✅ | B | 原始题面，**不含**本组附加说明 |
| `test` | string | ✅ | B | 可直接执行、内含 `check()` 的正确性测试块 |
| `inputs` | array | ✅ | A 生成，B 入库 | 见下 |
| `difficulty` | object | ⬜ | **仅 B 回填** | A 不维护此字段 |

`inputs[]`：

| 字段 | 类型 | 说明 |
|---|---|---|
| `tier` | string | `t1`…`t5`，由小到大 |
| `n` | int | 规模标识（列表长度/整数大小/节点数） |
| `gen` | string | `seed:20261023` 或 `tier:small_test_cases`，说明这份输入怎么来的 |
| `args_hash` | string | 规范化序列化后的 args 取 sha256 前 16 位；**配对比较只认它** |
| `desc` | string | 人类可读描述，可选 |

`difficulty`：

| 字段 | 类型 | 说明 |
|---|---|---|
| `baseline_ms` | float | 冻结档位上的基线耗时（`mean_trimmed_ms`） |
| `headroom` | float | 基线耗时 ÷ 参考最优解耗时，≥1.5 才算有优化空间 |
| `cv` | float | 重复测量变异系数，>0.10 标记不稳 |
| `category` | string | 题型标签，如 `list/loop`、`dp`、`graph` |
| `protocol_version` | string | 算出这些数时的协议版本 |

## 3. Result

```json
{
  "run_id": "20261127T2030_deepseek_v4_pro_n120_s42_a1b2c3d",
  "track": "optimize",
  "dataset": "effibench",
  "task_id": "effibench/137",
  "method": "ours",
  "protocol_version": "proto-2026.11",
  "status": "ok",
  "degraded": false,
  "degraded_from": null,
  "manifest": {
    "model": "deepseek-v4-pro",
    "model_version": "2026-11-20",
    "temperature": 0.2,
    "seed": 42,
    "dataset_hash": "sha256:...",
    "harness_commit": "a1b2c3d"
  },
  "time": {
    "mean_trimmed_ms": 12.3456,
    "median_ms": 12.1,
    "iqr_ms": 0.42,
    "cv": 0.031,
    "tiers": [
      {"tier": "t3", "n": 100000, "mean_trimmed_ms": 12.3456,
       "iqr_ms": 0.42, "cv": 0.031, "repeats": 12, "degraded": false}
    ]
  },
  "mem": {"peak_tracemalloc_mb": 3.214, "peak_rss_mb": 41.882},
  "speedup": 1.1712,
  "mem_ratio": 0.873,
  "code": "def count_up_to(n): ..."
}
```

### 3.1 `method` 枚举（与 F0–F4 对齐，不许新造）

| 值 | 含义 |
|---|---|
| `base` | 初始生成，不做任何优化 |
| `vanilla` | F0：只说「优化一下」，无任何执行反馈 |
| `f1` | 只给总耗时 |
| `f2` | 只给最慢用例耗时（论文等价） |
| `f3` | 只给热点行（中期做法） |
| `ours` | F4：热点区域 + 调用链 + 规模 + 内存 |

### 3.2 `status` 五类（判定有顺序，先命中先返回）

| 值 | 判定条件 |
|---|---|
| `api_failed` | 模型调用失败或空响应，代码未进入执行 |
| `incorrect` | 编译/导入失败，或 `check()` 断言失败 |
| `timeout` | 所有必测档位都用完预算仍未完成有效测量 |
| `resource_exceeded` | 超过内存上限，或被 OS/看门狗因资源原因终止 |
| `ok` | 以上都不成立 |

**关键约定**：降档后仍有有效档位 → `status=ok` 且 `degraded=true`、`degraded_from=<原档位>`。
「跑得久」永远不等于「错」，`timeout` 单独统计，不进加速比汇总。

### 3.3 时间口径

- 主口径 `mean_trimmed_ms`：每档重复 E=12，去掉最大最小各 2 个，剩 8 个取均值；
- 辅口径 `median_ms`：同一批 12 次的**中位数**，用于观察偏态，**不参与表格比较**；
- 报告、汇总、speedup 一律用 `mean_trimmed_ms`，避免两套数打架；
- E=12 是本组测量协议（对齐论文「多次执行 + 去极值」思路），论文是否明确写 E=12 待页码核对，报告里按「本组协议」表述。

### 3.4 内存口径

- `peak_tracemalloc_mb`：Python 堆峰值，可归因到行，用于 `%Opt_mem`；
- `peak_rss_mb`：进程驻留峰值，用于描述真实占用；
- 两者**不可互相换算**，不许用 RSS 差值当作分配量。

### 3.5 派生指标（汇总脚本算，禁止手填）

- `speedup = baseline.mean_trimmed_ms / candidate.mean_trimmed_ms`
- `mem_ratio = candidate.peak_tracemalloc_mb / baseline.peak_tracemalloc_mb`
- 配对条件：`task_id` + `tier` + `args_hash` 三者全一致；任一不同即 `invalid`，不进汇总。

## 4. 单位与取整

| 量 | 单位 | 保留 |
|---|---|---|
| 时间 | ms | 4 位小数 |
| 内存 | MB | 3 位小数 |
| 比率 | 无量纲 | 4 位小数 |
| 时间戳 | ISO 8601 带时区偏移 | — |

## 5. 字段归属（避免互相等）

| 内容 | 归属 |
|---|---|
| Task 基础字段、题库文件 | B |
| `Task.inputs` | A 生成 → B 入库 |
| `Task.difficulty` | B（调用 `harness.measure_task()` 后回填） |
| `Result.time/mem/status` | A（harness 唯一产出方） |
| `Result.manifest` 模型信息 | 调用方（组长 / D） |
| `manifest.harness_commit`、`protocol_version` | harness 自动补 |
| `speedup` / `mem_ratio` | 汇总脚本自动算 |

## 6. 数值基线与 golden

- 每一个 golden 记录必须写成三元组：**(数值, protocol_version, 机器标识)**；
- 口径一变，**新数值覆盖旧数值前先归档**，报告里并列「旧口径 / 新口径」两列，并说明差异来源；
- 禁止把旧口径的数字直接当新口径的验收标准。

## 7. 冻结流程

1. 本文件 v0 → 组长 + A 逐字段过一遍；
2. 组会通过后改名 v1，打 tag `schema-v1`；
3. 之后任何字段增删：PR + 迁移调用方 + 重跑 golden，三项齐了才合并。
