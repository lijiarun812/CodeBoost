#!/usr/bin/env python3
"""RSS 空载基线：什么时候能用、什么时候必须判「不可用」。

背景
----
`peak_rss_mb` 含解释器自身占用（本机空载约 **20.5 MB**），所以「省了多少内存」必须用
`peak_rss − 空载基线`。**基线是机器/解释器相关的量**，换台机器、换个 Python 版本都会变；
拿历史值硬扣，等于给 MU 列注入一个假信号。

规则（队员A 裁定，2026-11-28）
------------------------------
1. **正式批开始前必须尝试量一次当次基线**：同机、同解释器、同启动方式（`ensure()`）。
2. **基线有效性**四条同时满足：同 host、同 Python 主次版本、同 `protocol_version`、`max_age_days` 内。
3. **没有有效基线时：不停止整批**（停止只会白扔已花的模型调用），但
   - `rss_status = "unavailable"`，**RSS 门禁整体跳过**（不否决任何候选）；
   - **MU 扣基线列一律 `n/a`**，禁止用历史/他机基线算数字；
   - 结果里写 `rss_baseline: {status, reason, ...}`，报告必须标「本批 MU 不可用」。
4. **结论层**：没有有效基线的批次**只能出「时间 + peak_tracemalloc」的结论**，
   不得写任何「省了多少内存」。
5. 禁止跨机器复用（缓存里带 host/python，不匹配即失效）。

用法
----
    from eval.rss_baseline import ensure, load
    info = ensure()              # 需要时现量一次，返回 {status, value, reason, meta}
    info = load()                # 只读缓存（分析脚本用，不花时间）
    if info["status"] != "ok":
        mark_mu_unavailable(info["reason"])
"""

from __future__ import annotations

import datetime as _dt
import json
import platform
import socket
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from eval import harness  # noqa: E402

CACHE = REPO_ROOT / "results" / "rss_baseline.json"
MAX_AGE_DAYS = 7
PROBE_CODE = "class Solution:\n    def f(self, x):\n        return x\n"
PROBE_TEST = "assert solution.f(1) == 1\n"


def _now() -> _dt.datetime:
    return _dt.datetime.now()


def _meta() -> dict:
    return {
        "host": socket.gethostname(),
        "python": "%d.%d" % sys.version_info[:2],
        "platform": platform.platform(),
        "protocol_version": harness.PROTOCOL_VERSION,
        "how": "harness.measure_code(kind=test_block, entry=solution, e_reps=2, 最小程序)",
    }


def measure() -> float | None:
    """现量一次空载 RSS 峰值（MB）。失败返回 None。"""
    try:
        m = harness.measure_code(PROBE_CODE, PROBE_TEST, kind="test_block", entry="solution",
                                 e_reps=2, target_batch_s=0.02, max_reps=50)
    except Exception:  # noqa: BLE001
        return None
    if m.get("status") != "ok":
        return None
    return (m.get("mem") or {}).get("peak_rss_mb")


def _valid(meta: dict, max_age_days: int) -> tuple[bool, str]:
    """基线是否可用于「正式运行」。返回 (是否有效, 原因)。"""
    expect = _meta()
    if not meta:
        return False, "cache_missing_meta"
    if meta.get("host") != expect["host"]:
        return False, "host_mismatch(%s≠%s)" % (meta.get("host"), expect["host"])
    if meta.get("python") != expect["python"]:
        return False, "python_mismatch(%s≠%s)" % (meta.get("python"), expect["python"])
    if meta.get("protocol_version") != expect["protocol_version"]:
        return False, "protocol_mismatch(%s≠%s)" % (
            meta.get("protocol_version"), expect["protocol_version"])
    stamp = meta.get("measured_at")
    try:
        age = (_now() - _dt.datetime.fromisoformat(stamp)).days if stamp else None
    except ValueError:
        age = None
    if age is None:
        return False, "measured_at_missing"
    if age > max_age_days:
        return False, "stale(%d天>%d天)" % (age, max_age_days)
    return True, "ok"


def load(max_age_days: int = MAX_AGE_DAYS) -> dict:
    """只读缓存并判有效性，不现量。"""
    if not CACHE.exists():
        return {"status": "missing", "value": None, "reason": "cache_file_not_found", "meta": {}}
    try:
        doc = json.loads(CACHE.read_text(encoding="utf-8"))
    except ValueError:
        return {"status": "missing", "value": None, "reason": "cache_unreadable", "meta": {}}
    meta = doc.get("meta") or {}
    value = doc.get("peak_rss_mb")
    ok, reason = _valid(meta, max_age_days)
    if value is None:
        return {"status": "missing", "value": None, "reason": "cache_has_no_value", "meta": meta}
    if not ok:
        return {"status": "stale", "value": None, "reason": reason, "meta": meta}
    return {"status": "ok", "value": float(value), "reason": "ok", "meta": meta}


def ensure(force: bool = False, max_age_days: int = MAX_AGE_DAYS) -> dict:
    """拿到一个**有效**基线：优先用缓存，无效/强制则现量一次并写缓存。"""
    if not force:
        hit = load(max_age_days)
        if hit["status"] == "ok":
            return hit
        prior = hit["reason"]
    else:
        prior = "forced"
    value = measure()
    if value is None:
        return {"status": "missing", "value": None,
                "reason": "measure_failed(前次: %s)" % prior, "meta": _meta()}
    meta = dict(_meta(), measured_at=_now().isoformat(timespec="seconds"))
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps({"peak_rss_mb": value, "meta": meta}, ensure_ascii=False, indent=2),
                     encoding="utf-8")
    return {"status": "ok", "value": float(value), "reason": "measured_now(前次: %s)" % prior,
            "meta": meta}


def _selftest() -> int:
    print("== 1) 现量 ==")
    v = measure()
    assert v is not None and v > 0, v
    print("  ok: %.2f MB" % v)

    print("== 2) ensure 写缓存并判有效 ==")
    got = ensure(force=True)
    assert got["status"] == "ok" and CACHE.exists(), got
    print("  ok: %s / %s" % (got["reason"], got["meta"]["measured_at"]))

    print("== 3) 换宿主 -> 失效 ==")
    doc = json.loads(CACHE.read_text(encoding="utf-8"))
    doc["meta"]["host"] = "another-machine"
    CACHE.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    got = load()
    assert got["status"] == "stale" and "host_mismatch" in got["reason"], got
    print("  ok:", got["reason"])

    print("== 4) 过期 -> 失效 ==")
    ensure(force=True)
    doc = json.loads(CACHE.read_text(encoding="utf-8"))
    doc["meta"]["measured_at"] = (_now() - _dt.timedelta(days=MAX_AGE_DAYS + 1)).isoformat(
        timespec="seconds")
    CACHE.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    got = load()
    assert got["status"] == "stale" and got["reason"].startswith("stale"), got
    print("  ok:", got["reason"])

    print("== 5) 恢复成有效基线 ==")
    got = ensure(force=True)
    assert got["status"] == "ok", got
    print("  ok: %.2f MB" % got["value"])

    print("\nALL RSS BASELINE SELFTESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
