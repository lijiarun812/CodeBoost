# 审查：`optimize/` 行级诊断与 `eval/harness.py` 的边界

> 审查对象：`opt/hotspot-context` @ `7a3f4b1`（`optimize/` 8 个文件，931 行新增）
> 审查人：队员A（`eval/` 负责人）
> 审查方式：读全部代码 + 在 detached worktree 里跑他自己的测试 + 用真实 EffiBench 条目做集成探针
> 结论：**架构方向通过，但不并入 `eval/hotlines.py` 的实现；只统一入口。合并前必须修 5 项，其中 2 项在我这边。**

> **修订（2026-11-27 晚，C 提出后核实）**：本文 **F5 撤回**。`eval/schema.md:94` 的
> `degrade_reason` 枚举**包含 `fallback_f2`**，我此前读漏了，C 保留该值是对的。
> 现行裁定见 [`共享接口_行级诊断_v1.md`](共享接口_行级诊断_v1.md) §2：`degrade_reason`
> 按**目标档位**选词（→f2 用 `fallback_f2`，→f1 用 `no_hotspot`，截断用 `budget_exceeded`），
> 诊断自身的 `code_error/timeout/...` 只进 `diagnostics`，**不改 `Result.status`**。

---

## 一、先说通过的部分（这些做法比我们现有代码更稳）

| 项 | 评价 |
|---|---|
| `line_timing.profile_case()` 用 `subprocess.run(..., timeout=10, capture_output=True)` | ✅ 真隔离、真超时。`subprocess.run` 内部用 `communicate()` 并发读两个管道，**不会有 64KB 管道死锁**（比手写 `Popen` 好） |
| 代码通过 **stdin JSON** 传给子进程，不落盘 | ✅ 避开了「沙箱删文件进回收站」那个坑（我们单题冒烟曾堆 79 个文件） |
| 用户代码的 `print` 用 `contextlib.redirect_stdout(sys.stderr)` 挡掉 | ✅ stdout 只留给机器可读的 MARKER |
| 回传后做 **source_sha256 + 逐行行号 + 逐行原文** 三重校验 | ✅ 这是整套代码里最好的一处：能挡住「行号对不上代码」的静默错误，我们别的地方缺这个 |
| 显式 status：`timeout / process_error / bad_payload / line_mismatch / code_error / missing_dependency` | ✅ 不糊成一个 `ok=False` |
| `missing_dependency` 分支 | ✅ 本机没装 line_profiler 也不会崩 |
| 反馈文案自带「含剖析开销；不用于最终 speedup」 | ✅ 符合「诊断档必须自我声明」的要求 |
| `can_write_result=False`（完整 F4） | ✅ 正确，见第四节 |

实测：`python -m unittest optimize.test_feedback -v` → **17/17 通过**（3.6 s）；
`optimize.test_line_timing` → **3 通过 / 3 跳过**（跳过的正是三个真 `line_profiler` 用例）。

---

## 二、必须修的问题（按严重度）

### F1 ｜【高】EffiBench 上 F2–F4 全部静默降级为 F1 —— **根因在我的 harness，不在 C 的代码**

真实 EffiBench 条目探针（`problem_idx=250`）：

```
f2 -> effective: f1 | reasons: ["case_diagnostic_failed: NameError: name 'solution' is not defined"]
```

原因链：

- EffiBench 的 `test_case` 写的是 `assert solution.countUnivalSubtrees(...) == 393`，**用的是实例 `solution`**；
- 而数据里的代码定义的是 `class Solution`；
- `eval/harness.py:158`（`MODE == "cases"` 分支）只绑定 `ns["candidate"] = ns.get(ENTRY) or g.get(ENTRY)`，
  `ENTRY` 传 `"solution"` 时取不到实例 → 测试块 `NameError`；
- `build_feedback` 里这个异常被 `except Exception` 吞掉 → `effective=f1`。

**后果**：只要不修，F2/F3/F4 在主线数据集（EffiBench）上**永远不会出现**，消融表会显示「F4 = F1」，
而且**没有任何报错**——这是最危险的一类错误。

**归属**：我这边修（`test_block` 分支需 `ns["solution"] = ns[ENTRY]()`，即 `entry` 的语义是「类名」不是「实例名」）。
**在修好之前，F4 的任何 EffiBench 实测数字都不成立。**

### F2 ｜【高】子进程没有测试前导（PREAMBLE）与 tree/list helper

- `eval/harness.py:_build_child()` 对 `kind="test_block"` 会前置 `PREAMBLE`（`from typing import *`、`from collections import *` 等一整套）；
- `effilearner/effi_repro.py` 另外注入 `ListNode` / `TreeNode` / `build_tree`；
- C 的子进程只有 `namespace = {"__name__": ...}` 然后 `exec(code)` —— **两者都没有**。

**后果**：
1. 带类型注解（`List[int]`）或树/链表题 → `NameError` → `code_error` → F4 静默退化 `line_hits`；
2. 更隐蔽：**诊断执行的代码环境 ≠ 被测环境**，行号可能对应一次根本没跑通的执行。这违反「同一把尺子」的精神。

**归属**：C 改接口，我出 prelude。建议签名加 `prelude: str = ""`，由调用方传 `harness.PREAMBLE`；
tree/list helper 则应并入 `harness.PREAMBLE`（这样所有入口自动一致，不再各写一份）。

### F3 ｜【中】超时和被测用例的时长脱钩

`timeout_s=10` 是固定值，但 line_profiler 插桩会让同一段代码慢**数倍**。调用方明明已经有 `slow["ms"]`，却没用它推导超时。

**后果**：本来 2 s 的用例，插桩后 8–10 s → 边界超时 → 静默退化成 `line_hits`，而且日志里看不出「是因为太慢才没有计时」。

**建议**：`timeout_s = max(10.0, 20 * slow_ms / 1000)`，并把 `slow_ms` 写进 `diagnostics`，事后能判断阈值是不是太紧。

### F4 ｜【中】亚毫秒用例上的行级数字没有信息量

line_profiler 的计时粒度约 1 µs。我们主线数据集前 20 题的**单用例耗时是 0.02–0.9 ms**，
折成 tick 只有几十到几百个，摊到十几行上，每行的相对误差可达几十个百分点。

他自己的真实用例用的是 `time.sleep(0.02)`（20 ms）——**这恰恰掩盖了真实失效区间**。
他自己在消息里也提出「要有足够执行规模的离线用例」，但测试里没有把这条落成**硬门槛**。

**建议**：
1. 代码里加硬门槛：`case_ms < 2.0` 时直接降级 `line_hits`，`diagnostics` 记 `reason="case_too_small"`；
2. 补一个 0.3 ms 量级的用例，断言「同一份代码跑 3 次，top-1 热点行至少 2/3 次相同」。

### F5 ｜【中】`degrade_reason="fallback_f2"` 不在 schema 枚举里

`eval/schema.md:94` 的枚举是 `null` / `budget_exceeded` / `no_hotspot` / `unsupported…`。
`fallback_f2` 写出去就是**协议外取值**，汇总时会解析失败或静默漏掉。

**建议**：`f2` 退化统一映射到 `no_hotspot`（它本来就是「拿不到行级数据」）。

### F6–F9 ｜【低】四处小问题

| # | 问题 | 建议 |
|---|---|---|
| F6 | `cumulative_ms` 命名误导：line_profiler 的逐行时间**不含被调函数**，不是 cumulative | 改名 `line_time_ms`，或在文案里写明「不含被调用函数」 |
| F7 | `_retained_hotspot_context()` 用**子串匹配**判断上下文是否保留；重复行/相同文本会误命中 | 已有 `meta["regions"]`，直接用区间覆盖判定 |
| F8 | use_timing 但上下文被截断时，`effective="f3"` 而 `hotspot_mode="line_profiler_hits_only"`，两者语义冲突 | 二者取一：要么 mode 也退回 `line_hits`，要么 effective 保持 f4 只标降级原因 |
| F9 | 真计时测试在**没有装 line_profiler 的机器上全部 skip**（本机就是），全组只有 C 的机器验证过 | 加 `requirements-opt.txt`（`line_profiler>=5.1`），至少两台机器能跑；`tool_version` 已记，报告里要标 |

---

## 三、边界裁定（我作为 `eval/` 负责人给的结论）

| 争议点 | 裁定 | 理由 |
|---|---|---|
| 独立子进程 + 显式 status | ✅ **符合** | 甚至比我们现有手写 `Popen` 更稳 |
| 是否必须复用 harness 心跳 / 五类状态 | ❌ **不必须**（诊断档、10 s 有界），但**必须**：① 超时按用例时长推导（F3）；② status 词表与 harness 五类**显式映射并写进文档**，不许同名不同义 | 心跳是为「跑不完的长任务」设计的；诊断档本身有硬超时，加心跳是重复建设 |
| 行级毫秒数进提示词 | ⚠️ **有条件**：保留「诊断值 / 含开销 / 不用于 speedup」声明（已有）+ **补上「占已剖析行合计的 %」**；`case_ms < 2 ms` 时不许出现毫秒 | 绝对值会让模型盯着某个 0.0003 ms 的数字微优化；占比是稳定量 |
| 是否并入 `eval/hotlines.py` | ❌ **不并入实现**；✅ **统一入口**：`eval/hotlines.py` 加门面 `line_hotspots(mode="count" \| "time")`，`time` 后端 **lazy import** `optimize.line_timing` | eval 拥有「入口 + 字段契约」，optimize 拥有「实现」。既满足「一把尺子」，又不把原型代码搬进共享层 |
| 完整 F4 写正式 Result | ❌ **继续 `can_write_result=False`** | 三个前提未满足：① 空间反馈字段未定；② F1（harness 绑定）未修；③ `degrade_reason` 枚举未对齐 |

**关于「行级计时不能与普通运行耗时混算」**——这条他做对了：feedback 里两个数是分开写的
（先给「Slowest test case … Case execution time: X ms」来自 `measure_cases`，再给逐行诊断值），
没有相加、没有相除、没有拿行级和去冒充总时间。**保持这个写法。**

---

## 四、下一步的分工

### 我（A）先做，否则 F2–F4 无法在 EffiBench 上跑

1. `eval/harness.py`：`kind="test_block"` 时绑定 `ns["solution"] = ns[ENTRY]()`（修 F1），并加回归用例：
   EffiBench 条目 `measure_cases` 必须返回 `status="ok"`；
2. `requirements-opt.txt`：`line_profiler>=5.1`（修 F9），让至少两台机器能跑真计时；
3. `eval/hotlines.py` 只加门面（不搬实现）——**等你确认后再动**。

### C 改（按严重度）

1. F2：`profile_case(..., prelude="")`，调用方传 `harness.PREAMBLE`；
2. F3：超时按 `slow_ms` 推导 + 记录 `slow_ms`；
3. F4：`case_ms < 2` 硬门槛 + 0.3 ms 量级的排序稳定性测试；
4. F5：`degrade_reason` 映射到 `no_hotspot`；
5. F6–F8：命名与 mode 一致性。

### D 那边

空间反馈字段（`peak_tracemalloc_mb` / `peak_rss_mb` / `peak_rss − baseline`）未定之前，
F4 **不写正式 Result**、不进消融表——C 的 `can_write_result=False` 保持正确。

---

## 附：本次审查的原始证据

```
# 他的测试（detached worktree @ 7a3f4b1，本机）
optimize.test_feedback       17 tests  OK
optimize.test_line_timing     6 tests  OK (skipped=3：三个真 line_profiler 用例因未安装而跳过)

# EffiBench 集成探针（problem_idx=250）
f2              -> effective=f1, reasons=["case_diagnostic_failed: NameError: name 'solution' is not defined"]
f4（无 test 键） -> effective=f1, reasons=["case_diagnostic_failed: KeyError: 'test'"]

# 对照：harness 两个入口在同一题上的表现
measure_cases -> incorrect: NameError: name 'solution' is not defined
measure_code  -> incorrect（同上；补 solution = Solution() 后可正常测，见 peak 采样脚本）
```

> 备注：`build_feedback` 里访问 `task["test"]`，而 EffiBench 条目的字段是 `test_case`（没有 `test`）。
> 这是调用方契约不一致的第二个表现：**EffiBench 上 F2–F4 会因为字段名而不是算法而失效**，
> 建议 `task` 的字段在 schema 里定死（`test` 还是 `test_case`），不要两套并存。
