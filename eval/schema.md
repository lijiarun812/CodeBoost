# eval 数据契约（Task / Result）

> 状态：**v1 已冻结**，`protocol_version = proto-2026.11`（组长 v0 措辞 + 成员 A 实测字段合并）
> 变更规则：升 `protocol_version` → 迁移全部调用方 → 重跑 12 题 golden，三项齐了才合并。

## 1. 文件与命名

```
results/<track>/<dataset>/<model>/<run_id>.json
results/golden/golden_<timestamp>.json      # 12 题体检表
```

| 段 | 取值 | 谁定 |
|---|---|---|
| `track` | `repro`（论文复现）/ `opt`（优化）/ `effilearner`（EffiLearner 线） | 固定 |
| `dataset` | `humaneval` / `mbpp+` / `effibench` / `apps` | 固定 |
| `model` | 小写 slug，非字母数字换 `_`（`deepseek_v4_pro`）；标准解写 `canonical` | 组长 |
| `run_id` | `<YYYYMMDD>-<HHMMSS>-<model_slug>-s<seed>-<commit>-<task_id>` | harness 自动生成 |

`manifest.harness_commit` 取 `git rev-parse --short HEAD`；不在 git 仓库时写 `unknown`。

## 2. Task（一道题）

| 字段 | 类型 | 必填 | 谁填 | 说明 |
|---|---|---|---|---|
| `task_id` | string | ✅ | B | 全局唯一：`HumanEval/40`、`EffiBench/137` |
| `dataset` | enum | ✅ | B | 见上表 |
| `kind` | enum | ✅ | B | `humaneval`（调 `check(candidate)`）/ `test_block`（自包含测试块，EffiBench 用） |
| `entry_point` | string | ✅ | B | HumanEval 是函数名；EffiBench 是 `solution` |
| `task_name` | string | ⬜ | B | 人类可读名，仅展示用 |
| `code` | string | ✅ | B | 初始/标准代码，可直接 `exec` |
| `test` | string | ✅ | B | 可直接 `exec` 的正确性测试块（native 档） |
| `inputs` | array | ✅ | A 生成 → B 入库 | 输入阶梯，见下 |
| `difficulty` | object | ⬜ | **仅 B 回填** | A 不维护题库 |

`inputs[]`：

| 字段 | 类型 | 说明 |
|---|---|---|
| `tier` | string | `t1`…`t5`，由小到大 |
| `n` | int | 规模标识（列表长度/整数大小/节点数），`null` 表示不放大 |
| `gen` | string | `seed:20261023` / `native` / `tier:small_test_cases` |
| `args_hash` | string | 规范化 args 的 sha256 前 16 位；**配对比较只认它** |
| `test` | string | 该档使用的测试块（覆盖 Task.test），可选 |
| `desc` | string | 人类可读描述，可选 |

`difficulty`（B 用 `harness.calibrate_task()` 的结果回填）：

| 字段 | 说明 |
|---|---|
| `baseline_ms` | 冻结档位上的**去极值均值** `mean_trimmed_ms` |
| `headroom` | 基线耗时 ÷ 参考最优解耗时，≥1.5 才算有优化空间 |
| `cv` | 重复测量变异系数，>0.10 标记不稳 |
| `category` | 题型标签：`list/loop`、`dp`、`graph`… |
| `protocol_version` | 算出这些数时的协议版本 |

## 3. Result（一次实验记录）

```json
{
  "run_id": "20261127-182913-canonical-s42-10a23fa-HumanEval_31",
  "task_id": "HumanEval/31",
  "dataset": "humaneval",
  "method": "base",
  "started_at": "2026-11-27T18:29:13",
  "manifest": {
    "model": "deepseek-v4-pro", "model_version": "2026-11-20",
    "temperature": 0.2, "seed": 42,
    "dataset_hash": "sha256:1a2b3c4d5e6f7788",
    "harness_commit": "10a23fa", "protocol_version": "proto-2026.11"
  },
  "status": "ok",
  "error": null,
  "time": {
    "median_ms": 0.5753, "mean_trimmed_ms": 0.5787,
    "iqr": 0.0102, "cv": 0.0327,
    "raw_ms": [0.571, 0.582, "..."],
    "tiers": [{"tier": "t1", "n": null, "gen": "native", "args_hash": null,
               "status": "ok", "median_ms": 0.5753, "iqr": 0.0102,
               "cv": 0.0327, "reps": 459, "downgraded": false}]
  },
  "mem": {"peak_tracemalloc_mb": 0.0011, "peak_rss_mb": 19.773},
  "speedup": null, "mem_ratio": null,
  "downgraded": false, "protocol_version": "proto-2026.11",
  "code_source": "canonical"
}
```

| 字段 | 归属 | 说明 |
|---|---|---|
| `method` | 全员 | `base` / `effi`（**复现主线**：EffiLearner 三数字反馈 + 多轮）/ `vanilla`(F0) / `f1`(总耗时) / `f2`(最慢用例，PerfCodeGen 对照档) / `f3`(仅热点行) / `ours`(F4 热点区域) |
| `manifest` | A 生成，调用方填模型 | **缺一项不进汇总** |
| `status` | A | 五类，判定顺序见 §4 |
| `time` | A | 主口径 `mean_trimmed_ms`；`median_ms` 为**真中位数**（辅口径，不进表格比较）；`raw_ms` 必留 |
| `time.tiers` | A | 每次升档尝试都记一条，禁止“只用一档就下结论” |
| `mem` | A | 双口径，禁止 RSS 时间积分 |
| `speedup` / `mem_ratio` | 汇总脚本 | 相对 base 的比值，禁止手填 |
| `downgraded` | A | 是否因超时把重复次数降过档 |
| `code_source` | A | `canonical`（标准解）/ `candidate`（模型或候选代码） |

## 4. status 判定（先判“跑没跑完”，再判“对不对”）

```
1. timeout            心跳 30s 中断 / 单档预算超时（含降档后仍超时）
2. resource_exceeded  RSS 超过 --max-mem（默认 2048 MB）
3. api_failed         模型侧调用失败（重试后仍失败）——只出现在推理阶段
4. incorrect          代码能跑但测试不通过（含 SyntaxError、断言失败）
5. ok                 其他
```

> 铁律：`timeout` **不得**并入 `incorrect`。中期“跑得久=判错”就错在这一步。
> `downgraded=true` 但最终有有效档位 → `status=ok`，必须在报告里注明降过档。

## 5. 时间口径

- 每档重复 **E=12 批**，去掉最高/最低各 2 个，剩 8 个取均值 = `mean_trimmed_ms`（主口径）；
- `median_ms` 由同一批 12 个样本**另行计算真中位数**，只用于观察偏态；
- **计时档必须关闭 `tracemalloc`**，内存单独一趟测（见 §6）；
- 计时前先标定重复次数（目标 0.25s/批，`MIN_REPS=1`，单次 >1s 的大任务允许 `reps=1`）；
- E=12 是**本组协议**（对齐论文“多次执行 + 去极值”思路），论文是否明写 E=12 待页码核对，报告按「本组协议」表述。

## 6. 内存口径

- `peak_tracemalloc_mb`：Python 堆峰值，可归因到行，用于 `%Opt_mem`；
- `peak_rss_mb`：进程驻留峰值，用于描述真实占用；
- 内存档**只取峰值、时间丢弃**（`tracemalloc` 会让被测代码慢 10–15 倍，实测 `mem/time ≈ 10–20x`）；
- 两者不可互相换算，禁止用 RSS 差值当分配量，禁止再用 MB·s 积分。

## 7. 派生指标与配对

- `speedup = baseline.mean_trimmed_ms / candidate.mean_trimmed_ms`
- `mem_ratio = candidate.peak_tracemalloc_mb / baseline.peak_tracemalloc_mb`
- 配对条件：`task_id` + `tier` + `args_hash` 三者全一致；任一不同即 `invalid`，不进汇总。

## 8. 单位与取整

| 量 | 单位 | 保留 |
|---|---|---|
| 时间 | ms | 4 位小数 |
| 内存 | MB | 3–4 位小数 |
| 比率 | 无量纲 | 4 位小数 |
| 时间戳 | ISO 8601 | 到秒 |

## 9. 字段归属（避免互相等）

| 内容 | 归属 |
|---|---|
| Task 基础字段、题库文件 | B |
| `Task.inputs` | A 生成 → B 入库 |
| `Task.difficulty` | B 调 `harness.calibrate_task()` 后回填，A 不碰 |
| `Result.time / mem / status / run_id` | A（harness 唯一产出方） |
| `Result.manifest` 模型信息 | 调用方（组长 / D） |
| `speedup` / `mem_ratio` | 汇总脚本自动算 |

## 10. golden（12 题体检表）

每条 golden 记录必须写成三元组：**(数值, protocol_version, 机器标识)**，并锁四件事：

1. `protocol_version` 为当前版本；
2. 12 题全部 `status=ok`；
3. 每题 `cv ≤ 10%`；
4. 新值相对本次基线浮动 ≤ 20%（同机、同日）。

> 旧口径的 `HE/40 = 1.171x` 是 `legacy-psutil` 产物，只能进对照列，不得当验收阈值。

## 11. 冻结流程与变更记录

v0（组长）→ v1（合并 A 实测字段）→ 冻结打 tag `schema-v1`。

| 版本 | 变更 |
|---|---|
| v0 | 初稿：字段表、method 枚举、status 顺序、字段归属 |
| v1 | 采纳实测字段：`kind` / `time.tiers`（升档尝试）/ `downgraded` / `code_source` / `started_at`；`run_id` 改为实际格式；删除 v0 的“median 与 mean 同值兼容”表述（已修真中位数）；协议号统一 `proto-2026.11` |
| 待定 | 进程内 E=12（省 10× 启动开销），需先做「进程内 vs 跨进程 CI 宽度」对照再定 |
