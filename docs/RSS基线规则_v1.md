# RSS 空载基线规则 v1（裁定）

> 裁定人：队员A（`eval/` 负责人） · 2026-11-28 · 实现：[`eval/rss_baseline.py`](../eval/rss_baseline.py)（5 条自测全绿）
> 回应 C 的第 2 问：**没有可核对的当次环境基线时，停止正式运行？还是记录原因并跳过 RSS 门禁？**

---

## 一、结论：**记录原因 + 跳过门禁，不停止整批**

理由很简单：**基线缺失伤的是「MU 那一列」，不是整批数据**。停止运行只会白扔已经花掉的模型调用，
而时间列、`peak_tracemalloc` 列照样有效。

代价是「这一批不能谈内存」——所以规则的关键不在「跑不跑」，而在**判据和报告怎么标**。

| 情形 | 停不停 | 门禁 | MU 扣基线列 | 报告 |
|---|---|---|---|---|
| 基线有效（同机/同 Python/同协议/7 天内） | 正常跑 | 生效 | 出数字 | 正常 |
| 基线缺失 / 过期 / 换机 / 换 Python | **不停** | **跳过（不否决任何候选）** | **一律 `n/a`** | 标「本批 MU 不可用：<原因>」 |
| 有基线但该题 `peak_rss − 基线 < 5 MB` | 不停 | 跳过 | `n/a（无 ≥5 MB 的题）` | 标「该题 RSS 不可分辨」 |

**硬红线**：`excluded`/`n/a` 之外，**禁止**用历史基线或他机基线去算「扣基线 MU」。
20.5 MB 是「这台机器 + 这个 Python」解释器自己的占用，换台机器就变，硬扣等于给 MU 注入假信号。

---

## 二、基线怎么算「有效」

四条同时满足才算有效（`eval/rss_baseline.py::_valid`）：

| 条件 | 判据 | 失效原因串 |
|---|---|---|
| 同宿主 | `gethostname()` 相同 | `host_mismatch(...)` |
| 同 Python 主次版本 | `3.13` ≠ `3.12` | `python_mismatch(...)` |
| 同协议版本 | `proto-2026.11` | `protocol_mismatch(...)` |
| 够新 | `measured_at` 在 **7 天**内 | `stale(8天>7天)` |

缓存文件 `results/rss_baseline.json` 现在带 `meta`（host/python/platform/protocol/measured_at/how）；
**旧格式（只有 `peak_rss_mb`）会被判 `cache_missing_meta` → 视为无效**，这是故意的。

---

## 三、怎么用（三行）

```python
from eval.rss_baseline import ensure

info = ensure()                       # 缓存有效就用缓存；无效/过期则现量一次并写回
base_rss = info["value"]              # 不可用时是 None

from eval.space import pareto_accept
dec = pareto_accept(base, cand, rss_baseline_mb=base_rss)   # None → rss_status="unavailable"
```

`pareto_accept(..., rss_baseline_mb=None)` 的行为（已写进自测 13）：

- `rss_status = "unavailable"`，**门禁整体跳过**（不否决任何候选）；
- 时间轴与 `peak_tracemalloc` 轴照常工作；
- `reasons` 里写 `RSS 门禁跳过（unavailable）`。

---

## 四、正式运行前的三条动作（给 D / 组长）

1. 批开始前跑一次 `ensure()`，把返回的 `status/reason/value` 写进该批的 `manifest`；
2. 若 `status != "ok"`：该批**只出「时间 + peak_tracemalloc」结论**，报告里注明
   「MU 扣基线不可用：<reason>」；
3. 换机器/重装 Python 后**必须** `ensure(force=True)` 重量一次，不要沿用缓存。

---

## 五、自测（`python eval/rss_baseline.py`）

1. 现量得到正数；
2. `ensure(force=True)` 写缓存并判有效；
3. 改 host → `stale / host_mismatch`；
4. 改 `measured_at` 到 8 天前 → `stale(8天>7天)`；
5. 恢复后有效。

配套：`eval/space.py` 自测 13 覆盖「基线传 None → 门禁跳过、仍能按时间判定」。
