# 队员A 工作总结（测量基础设施 / 复现线）

> 交付分支：`a/measure-infra`；尺子版本：`protocol_version = v1`
> 本文档回答三个问题：**为什么要有这套东西、已经做到什么程度、还剩什么问题。**

---

## 一、目的（为什么必须做这件事）

中期汇报暴露了三个“尺子问题”，老师也点名了两条（180s 超时、反馈粒度）：

| 中期问题 | 后果 |
|---|---|
| 计时在噪声里 | 12 题里 6 题基线 <0.15ms，三组加速比 1.038–1.065 落在抖动范围内，得不出结论 |
| 内存用 RSS 时间积分 | 328 条记录里 101 条（30.8%）final 积分为 0 → “内存下降 100%”是假的 |
| 硬超时 | 跑得久被判成“答错”，合法的大任务被误杀 |
| 只有一把嘴上的尺子 | 三套实现（`effi_repro` 的 psutil、旧包 `sandbox.py` 的 `time.time()`、新 harness）混用，数字永远对不上 |

所以本工作的目标只有一句话：**做出全组唯一、可复现、能区分“慢”和“错”的尺子**，让 B/C/D 的数字可以放在同一张表里比较。

---

## 二、实现了什么（5 个文件）

| 文件 | 职责 | 关键点 |
|---|---|---|
| `eval/harness.py` | **唯一测量实现** | E=12 去极值；计时与内存**分开测**（tracemalloc 插桩会放慢 3–6 倍）；5s 心跳 / 30s 看门狗；超时降档重试；status 五类；`measure_task()` / `calibrate_task()` 两个入口 |
| `eval/inputs.py` | 输入阶梯 | EffiBench 1000/1000 题都有 `test_case_generator` → native → gen200 → gen1000；生成器**隔离执行**（它自带参考 Solution，否则会覆盖候选代码）；固定 seed，`args_hash` 可复现；缺参考解的生成器自动跳过 |
| `eval/stats.py` | 统计与噪声地板 | 中位数/IQR/CV；**配对 bootstrap 95% CI**；噪声地板 `1 + 2·CV`；替代主观 1.3x 阈值 |
| `eval/legacy_retest.py` | 旧口径 vs 新口径 | 用结果 JSON 里的 `final_code` 重测，零 API；回答“口径差多少”，不重新解释效果 |
| `eval/schema.md` | 字段契约 | Task / Result、status 判定顺序、字段归属、`protocol_version` 规则、golden 三元组、目录命名 |

配套文档：`docs/口径切换说明.md`（旧尺子 vs 新尺子 + 12 题基线 + 数据集缺陷清单）。

---

## 三、验证结果（可复跑）

### 3.1 自检（`python eval/harness.py --selftest`）

| 用例 | 结果 |
|---|---|
| 去极值均值（12 个样本去 2/2） | 6.5 ✓ |
| 端到端（synthetic） | median 0.029ms，CV 5.7%，双口径内存均非空 ✓ |
| 错误答案 | `status=incorrect` ✓ |
| 卡死 1.5s / 看门狗 0.5s | `status=timeout`，error=`heartbeat stale 0.5s` ✓ |

> 最后一条就是回应老师“180s 超时”的答案：**慢 ≠ 错**，且超时是独立状态、单独统计。

### 3.2 12 题体检表（`results/golden/golden_20261127-174616.json`）

- 12/12 `status=ok`，CV **1.4%–5.2%**；
- 最大耗时 `HE/147 = 25.76 ms`（tracemalloc 3.69 MB / RSS 29.0 MB），是唯一有内存信号的题；
- 其余 0.0015–0.76 ms，靠重复放大（最多 4 万次）压住噪声。

### 3.3 输入阶梯（`python eval/inputs.py --selftest`）

`EffiBench/3`：native 0.148ms → gen200 **7.1ms** → gen1000 **24.2ms**（放大后进入可测区间）；
`EffiBench/3` 在 harness `--tier auto` 下实测 t1 0.0125ms → 自动升 t2 **5.48ms**（停止）。

### 3.4 统计（`python eval/stats.py --selftest`）

真实 +20%：ratio 1.198、CI [1.194, 1.202]、地板 1.062 → `real=True`；
噪声 +2%：ratio 1.022、CI [1.013, 1.031]、地板 1.088 → `real=False`。

### 3.5 旧口径 vs 新口径（12 题，4 模型 × 3 题）

新/旧比值 **1.67x–2.04x，中位数 1.78x**。差异来源：函数包装（不再每轮 `exec`）、E=12 去极值、
机器状态不同。**结论：新旧数字绝对不可混表**，见 `docs/表格_旧口径vs新口径.md`。

---

## 四、还没解决的问题（如实列出）

| # | 问题 | 影响 | 建议 |
|---|---|---|---|
| 1 | **没有真正的“输入规模放大”** | 现在只能放大**用例条数**（gen200/1000），不能放大单条用例的输入（如 n 从 100 到 10⁶） | 需要按题写构造器 → 属 B 的 `inputs` 构造工作 |
| 2 | **EffiBench 数据缺陷**：`/100` native 传 list 而非 TreeNode，生成器又缺 `class Solution` | 该题两档都不可用 | B 在题库里剔除或补 `build_tree()` |
| 3 | **没有行级耗时** | 老师要求“测每行时间”，我们现在只有行**执行次数**（`eval/hotlines.py`），且 `line_profiler` 未安装 | 装 `line_profiler` 后接 C 的 `profiler.py`；未装时只能说明局限 |
| 4 | **schema 未与组长 v0 合并** | 组长 `582903d` 一直没推上来，两边字段可能有措辞差异 | 推送后按“措辞用 v0、字段用 v1”合并，再冻结 |
| 5 | **3 seed × 3 采样未跑** | 协议里要求的多随机种子统计还没做，目前只有单次 E=12 | 属 M2 全量实验阶段，等题库冻结 |
| 6 | 旧口径只重测了 12 题 | 对照样本偏少 | 扩到 24–30 题即可关闭（命令已给，零 API） |

---

## 五、交接（给别人怎么用）

### B（题库/难度）
```python
from eval import harness
from eval import inputs
task = harness.load_tasks("effibench", "datasets/dataset.json", ids=["3"])[0]
task["generator"] = ...                      # load_tasks 已带
d = harness.calibrate_task(task)             # -> {"difficulty": {baseline_ms, cv, ...}}
# 用新尺子量 gen200 档，避免“太简单”：
plan = inputs.tiers(task, "t2")
res = harness.measure_task(task, tier_plan=plan)
```
题库里的 <20ms 题会被 `baseline_ms` 自动筛掉；`difficulty` 由 B 回填，A 不维护。

### C（反馈构造）
- 直接用 `harness.measure_task(task, code=候选)` 拿 `time.median_ms` 与 `mem` 双口径；
- 行级时间/内存归因归 C（`optimize/profiler.py`），A 只提供测量与 `hotlines` 的次数代理。

### D（Ours v2）
- 联合目标用 `time.median_ms` + `mem.peak_tracemalloc_mb/peak_rss_mb`；
- Pareto 判据建议：时间改善 ≥5% 且内存回退 ≤10%，或内存改善 ≥10% 且时间回退 ≤10%；
- 注意：A 的 harness **不做** Pareto，只给数。

### 任何人改 `eval/harness.py` 的门槛
```powershell
python eval\harness.py --selftest
python eval\harness.py --dataset humaneval --tasks 31,40,55,59,60,63,96,120,123,130,139,147 --out-dir results/golden
```
两项都过、且 12 题 CV ≤ 10% 才能合并；不满足就是尺子被改坏。

---

## 六、一句话总结

**中期是“拿别人的结果在本机重放”，这次是把尺子本身做对**：同一个数字，先说清怎么测的、抖动多大、
超时怎么算，再谈方法有没有效。剩下的三件事（真·输入放大、行级耗时、schema 合并）都要等
B 的题库、C 的 profiler 和组长的 v0 就位。

