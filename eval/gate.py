#!/usr/bin/env python3
"""F4 完整性闸门：把 `docs/F4完整性闸门_v1.md` 的 G1–G5 变成可执行检查。

给 C / Codex 用：生成反馈之后、写 Result 之前调一次 `check()`；
`passed=False` 就不许写 `method="ours"`，按实际生效档位写 `f3/f2/f1` + `degrade_reason`。

两条边界（2026-11-28 裁定，已写进文档 §六）
------------------------------------------
① `case_ms < 2`：**允许「不适用」通过 G1**，但必须同时满足
   - `case_ms` 有实测值且 < 2.0；
   - `diagnostics.not_applicable.profiler_overhead_ratio == "case_too_small"`；
   - 该记录只能写 `f3`（`degrade_reason="no_hotspot"`），**不能写 `ours`**。
   「不适用」≠「失败」：前者进 `not_applicable`，后者进 `error`。
② G2 只禁**插桩数字**的绝对毫秒；`harness.measure_cases()` 测出的**最慢用例耗时必须保留**。

用法
----
    from eval.gate import check
    rep = check(diagnostics=diag, feedback=text, result_status="ok", harness_status="ok",
                prompt_hash_f3=h3, prompt_hash_f4=h4, method="ours")
    if not rep["passed"]:
        log(rep["failures"]);  method = rep["suggested_method"]

自测：`python eval/gate.py`（14 条）
"""

from __future__ import annotations

import re

REQUIRED_KEYS = ("hotspot_mode", "case_ms", "profiler_overhead_ratio", "space_status")
CASE_MS_FLOOR = 2.0
MS_TOKEN = re.compile(r"\d+(\.\d+)?\s*ms\b", re.IGNORECASE)
PCT_TOKEN = re.compile(r"\d+(\.\d+)?\s*%")
LINE_HEADER = "Hot lines"
CASE_HEADER = "Slowest test case"
DENOM_HINT = "已剖析行时间之和"


def _split_blocks(feedback: str) -> tuple[str, str]:
    """把反馈切成（最慢用例区块, 逐行区块）。没有逐行区块时第二段为空串。"""
    idx = feedback.find(LINE_HEADER)
    if idx < 0:
        return feedback, ""
    return feedback[:idx], feedback[idx:]


def check(diagnostics: dict, feedback: str, *,
          result_status: str | None = None,
          harness_status: str | None = None,
          prompt_hash_f3: str | None = None,
          prompt_hash_f4: str | None = None,
          formal_task: bool = True,
          method: str | None = None) -> dict:
    """返回 {passed, failures:[{code, detail}], suggested_method}。"""
    diag = diagnostics if isinstance(diagnostics, dict) else {}
    text = feedback or ""
    case_block, line_block = _split_blocks(text)
    na = diag.get("not_applicable") if isinstance(diag.get("not_applicable"), dict) else {}
    lt = diag.get("line_timing") if isinstance(diag.get("line_timing"), dict) else {}
    fails: list[dict] = []

    # —— G1 四个键齐全（允许「不适用通道」）
    for key in REQUIRED_KEYS:
        if diag.get(key) is not None:
            continue
        if key in na:
            continue
        fails.append({"code": "G1", "detail": "缺 %s 且未写 not_applicable" % key})
    if diag.get("profiler_overhead_ratio") is None and na.get("profiler_overhead_ratio") == "case_too_small":
        case_ms = diag.get("case_ms")
        if not isinstance(case_ms, (int, float)) or case_ms >= CASE_MS_FLOOR:
            fails.append({"code": "G1", "detail": "声称 case_too_small，但 case_ms=%r 不满足 <%.1f"
                                                     % (case_ms, CASE_MS_FLOOR)})

    # —— G2 插桩数字不得是绝对毫秒
    numbers_in = lt.get("numbers_in")
    if numbers_in not in ("percent", "hits_only"):
        fails.append({"code": "G2", "detail": "line_timing.numbers_in 必须是 percent 或 hits_only"})
    elif numbers_in == "percent":
        if not line_block:
            fails.append({"code": "G2", "detail": "numbers_in=percent 但反馈里没有逐行区块"})
        if MS_TOKEN.search(line_block):
            fails.append({"code": "G2", "detail": "逐行区块出现绝对毫秒（必须改成百分比）"})
        if DENOM_HINT not in line_block:
            fails.append({"code": "G2", "detail": "逐行区块未注明分母「%s」" % DENOM_HINT})
        if PCT_TOKEN.search(case_block):
            fails.append({"code": "G2", "detail": "最慢用例区块出现百分比（应只给 harness 的毫秒）"})
    else:  # hits_only
        if line_block and (MS_TOKEN.search(line_block) or PCT_TOKEN.search(line_block)):
            fails.append({"code": "G2", "detail": "numbers_in=hits_only 但逐行区块出现毫秒/百分比"})
    case_ms = diag.get("case_ms")
    if isinstance(case_ms, (int, float)) and case_ms < CASE_MS_FLOOR:
        if line_block and (MS_TOKEN.search(line_block) or PCT_TOKEN.search(line_block)):
            fails.append({"code": "G2", "detail": "case_ms<%.1f 时逐行区块不许有任何数字" % CASE_MS_FLOOR})

    # —— G3 空间字段诚实
    space = diag.get("space_status")
    if space != "measured":
        if space is None:
            pass  # 已被 G1 抓
        elif not re.search(r"n/?a|不可分辨|unavailable", text, re.IGNORECASE):
            fails.append({"code": "G3", "detail": "space_status=%s 但反馈没写 n/a 或「不可分辨」" % space})

    # —— G4 诊断不得改正式状态
    if result_status is not None and harness_status is not None and result_status != harness_status:
        fails.append({"code": "G4", "detail": "Result.status=%s 与 harness=%s 不一致"
                                               % (result_status, harness_status)})

    # —— G5 与 F3 可区分
    if prompt_hash_f3 is not None and prompt_hash_f4 is not None and prompt_hash_f3 == prompt_hash_f4:
        fails.append({"code": "G5", "detail": "F4 与 F3 的提示词哈希相同（视为退化成 F3）"})

    passed = not fails
    if passed and method == "ours":
        # 允许写 ours：但 case_too_small 的记录不行（见 §六-①）
        if diag.get("profiler_overhead_ratio") is None:
            passed = False
            fails.append({"code": "G1", "detail": "不适用通道只允许写 f3，不能写 ours"})
    # G6：闸门只判「反馈完整性」，不判「这是不是正式题目」。
    # 构造的诊断用例即使完整性全过，也不得写 method=ours（否则消融表会混进假样本）。
    if method == "ours" and not formal_task:
        passed = False
        fails.append({"code": "G6", "detail": "非正式题目（formal_task=False）不得写 method=ours"})
    if not formal_task:
        suggested = "diagnostic"
    else:
        suggested = method if passed else ("f3" if diag.get("hotspot_mode") != "none" else "f2")
    return {"passed": passed, "failures": fails, "suggested_method": suggested}


# ---------------------------------------------------------------------------
# 自测
# ---------------------------------------------------------------------------
def _good_diag(**over):
    d = {"hotspot_mode": "line_profiler", "case_ms": 12.3, "profiler_overhead_ratio": 2.83,
         "space_status": "measured", "not_applicable": {},
         "line_timing": {"numbers_in": "percent", "denominator": "profiled_lines_sum"}}
    d.update(over)
    return d


GOOD_FB = ("Slowest test case (measured by harness, not instrumented):\n"
           "assert solution.f(1) == 2\nCase execution time: 12.3450 ms.\n\n"
           "Hot lines (占已剖析行时间之和；line_profiler 插桩值，不可与运行耗时比较):\n"
           "L42: 31.2% | 118 hits | total += x\n")


def _selftest() -> int:
    def T(name, diag, fb, expect_pass, **kw):
        rep = check(diag, fb, **kw)
        ok = rep["passed"] is expect_pass
        print("%-52s -> %s%s" % (name, "PASS" if rep["passed"] else "FAIL",
                                 "" if ok else "   <<< 断言不符"))
        for f in rep["failures"]:
            print("     %s: %s" % (f["code"], f["detail"]))
        assert ok, (name, rep)
        return rep

    T("1) 规范反馈全过", _good_diag(), GOOD_FB, True,
      result_status="ok", harness_status="ok", prompt_hash_f3="a", prompt_hash_f4="b", method="ours")

    T("2) 缺 profiler_overhead_ratio 且无 not_applicable",
      {k: v for k, v in _good_diag().items() if k != "profiler_overhead_ratio"}, GOOD_FB, False)

    d = _good_diag(profiler_overhead_ratio=None, case_ms=0.9,
                   not_applicable={"profiler_overhead_ratio": "case_too_small"},
                   line_timing={"numbers_in": "hits_only"})
    fb = ("Slowest test case (measured by harness):\nassert x\nCase execution time: 0.9000 ms.\n\n"
          "Hot lines (按执行次数排序):\nL42: 118 hits | total += x\n")
    T("3) case_too_small 走不适用通道 + f3 -> 通过", d, fb, True, result_status="ok", harness_status="ok",
      prompt_hash_f3="a", prompt_hash_f4="b", method="f3")

    rep = T("4) case_too_small 却想写 ours -> 拦", d, fb, False,
            result_status="ok", harness_status="ok", prompt_hash_f3="a", prompt_hash_f4="b", method="ours")
    assert rep["suggested_method"] == "f3", rep

    T("5) 谎称 case_too_small 但 case_ms=12.3",
      _good_diag(profiler_overhead_ratio=None, case_ms=12.3,
                 not_applicable={"profiler_overhead_ratio": "case_too_small"},
                 line_timing={"numbers_in": "hits_only"}), fb, False)

    T("6) 逐行区块出现毫秒",
      _good_diag(), GOOD_FB.replace("L42: 31.2%", "L42: 0.0031 ms"), False)

    T("7) 逐行区块少写分母", _good_diag(),
      GOOD_FB.replace("占已剖析行时间之和；", ""), False)

    T("8) 最慢用例区块出现百分比",
      _good_diag(), GOOD_FB.replace("Case execution time: 12.3450 ms.",
                                    "Case execution time: 12.3450 ms (31%)."), False)

    T("9) case_ms<2 但逐行区块仍有百分比",
      _good_diag(case_ms=1.2, profiler_overhead_ratio=None,
                 not_applicable={"profiler_overhead_ratio": "case_too_small"}),
      GOOD_FB.replace("12.3450", "1.2000"), False)

    T("10) space_status 非 measured 却没写 n/a",
      _good_diag(space_status="below_floor"), GOOD_FB.replace("12.3450", "12.3450"), False)

    T("11) space_status 非 measured 且写了 n/a -> 通过",
      _good_diag(space_status="below_floor"),
      GOOD_FB + "\nProgram peak memory: n/a（本题空间不可分辨）\n", True,
      result_status="ok", harness_status="ok", prompt_hash_f3="a", prompt_hash_f4="b", method="ours")

    T("12) Result.status 与 harness 不一致",
      _good_diag(), GOOD_FB, False, result_status="incorrect", harness_status="ok")

    T("13) F4 与 F3 提示词哈希相同",
      _good_diag(), GOOD_FB, False,
      result_status="ok", harness_status="ok", prompt_hash_f3="same", prompt_hash_f4="same")

    T("14) numbers_in 缺失",
      _good_diag(line_timing={}), GOOD_FB, False)

    T("15) 构造诊断用例想写 ours -> 拦（G6）",
      _good_diag(), GOOD_FB, False,
      result_status="ok", harness_status="ok", prompt_hash_f3="a", prompt_hash_f4="b",
      formal_task=False, method="ours")

    rep = T("16) 构造诊断用例 + 不写 method -> 完整性通过，建议 diagnostic",
            _good_diag(), GOOD_FB, True, result_status="ok", harness_status="ok",
            prompt_hash_f3="a", prompt_hash_f4="b", formal_task=False, method=None)
    assert rep["suggested_method"] == "diagnostic", rep

    print("\nALL GATE SELFTESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
