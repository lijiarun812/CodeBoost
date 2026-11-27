#!/usr/bin/env python3
"""输入阶梯（对应老师意见 6 的“数据集太简单”与优化计划 8.2 的 inputs 字段）。

EffiBench 每题自带 `test_case_generator`，里面有一个 `test_generated_test_cases(n)`
能生成 n 条随机用例。所以本模块的“升档”是**放大生成用例数**（t1 → gen200 → gen1000），
而不是放大单条用例的输入规模——后者需要按题写构造器，属于 B 的题库工作。

两个必须防的坑（已在代码里处理）：
1. 生成器脚本内部**自带一份参考 Solution**，直接 exec 会覆盖候选代码 → 隔离到独立命名空间执行；
2. 生成器用 `random` 且未播种 → 固定 SEED，保证 `args_hash` 可复现。

对外接口：
    tiers(task, mode="auto")   -> [{tier, n, gen, args_hash, test}]
    build(task, tier_name)     -> 测试代码字符串
    auto_plan(task)            -> 由快到慢的档位列表（供 harness 升档用）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEED = 20261127
NOISE_FLOOR_MS = 2.0

# 档位定义：tier 名 -> 生成用例条数（None = 用数据集自带的 small_test_cases）
TIER_DEFS = {
    "effibench": [("t1", None), ("t2", 200), ("t3", 1000)],
    "humaneval": [("t1", None)],
    "mbpp+": [("t1", None)],
    "apps": [("t1", None)],
}

_MAIN_BLOCK = re.compile(r"\nif\s+__name__\s*==\s*['\"]__main__['\"]\s*:")


def _strip_main(src: str) -> str:
    """去掉生成器脚本末尾的 `if __name__ == '__main__':` 块。"""
    m = _MAIN_BLOCK.search(src)
    return src[:m.start()] if m else src


def _gen_src(task):
    return _strip_main(task.get("generator") or task.get("test_case_generator") or "")


def _hash(*parts):
    h = hashlib.sha256()
    for p in parts:
        h.update(str(p).encode("utf-8"))
    return "sha256:" + h.hexdigest()[:16]


def build(task, tier_name):
    """构造某一档的测试代码字符串。"""
    defs = dict(TIER_DEFS.get(task.get("dataset", "humaneval"),
                              [("t1", None)]))
    if tier_name not in defs:
        raise KeyError("未知档位 %s（可选 %s）" % (tier_name, list(defs)))
    n = defs[tier_name]
    if n is None:  # native：直接用数据集自带测试
        return task["test"]

    gen = _gen_src(task)
    if not gen.strip():
        # 没有生成器就退回 native（并在 args_hash 里标注）
        return task["test"]
    if "class Solution" not in gen:
        # 生成器若没自带参考 Solution，期望值就只能用候选代码算——那会“自己考自己”，
        # 一律不敢用（EffiBench/100 就是这种残缺生成器）。
        raise ValueError("generator_missing_solution")

    return (
        "if '_gen_cache' not in globals():\n"
        "    import random as _r\n"
        "    _r.seed(%d)\n"
        "    _GEN_SRC = %r\n"
        "    _ns = {}\n"
        "    exec(_GEN_SRC, _ns)  # 注意：不能写 compile()——PREAMBLE 里 `from re import *` 会把内置 compile 遮蔽\n"
        "    _gen_cache = _ns['test_generated_test_cases'](%d)\n"
        "solution = Solution()\n"
        "for _ln in _gen_cache:\n"
        "    exec(_ln, {'solution': solution})\n"
        % (SEED, gen, n)
    )


def tiers(task, mode="auto"):
    """返回可用的档位列表（每个含 tier/n/gen/args_hash/test）。"""
    dataset = task.get("dataset", "humaneval")
    names = [name for name, _ in TIER_DEFS.get(dataset, [("t1", None)])]
    if mode != "auto":
        names = [mode] if mode in names else ["t1"]
    out = []
    for name in names:
        n = dict(TIER_DEFS[dataset])[name]
        try:
            test = build(task, name)
        except Exception as e:  # noqa: BLE001
            out.append({"tier": name, "n": n, "gen": "seed:%d" % SEED,
                        "args_hash": _hash("build-failed", name, str(e)),
                        "test": task["test"], "error": str(e)[:200]})
            continue
        out.append({
            "tier": name,
            "n": n,
            "gen": "native" if n is None else "seed:%d" % SEED,
            "args_hash": _hash(test, SEED, n if n is not None else "native"),
            "test": test,
        })
    return out


def auto_plan(task):
    """升档顺序：native → gen200 → gen1000（humaneval 只有一档；残缺生成器的档位剔除）。"""
    usable = [t for t in tiers(task, "auto") if not t.get("error")]
    return usable or tiers(task, "auto")[:1]


def _selftest():
    data = json.loads((ROOT / "datasets" / "dataset.json").read_text(encoding="utf-8"))
    raw = next(x for x in data if str(x["problem_idx"]) == "3")
    task = {
        "task_id": "EffiBench/3",
        "dataset": "effibench",
        "kind": "test_block",
        "entry_point": "solution",
        "code": raw["canonical_solution"],
        "test": raw["small_test_cases"],
        "generator": raw["test_case_generator"],
    }
    print("== tiers ==")
    ts = tiers(task)
    for t in ts:
        print("  %-4s n=%-5s gen=%-12s %s" % (t["tier"], t["n"], t["gen"], t["args_hash"]))
    assert len(ts) == 3, ts
    assert ts[0]["args_hash"] != ts[1]["args_hash"], "native 与 gen 档 hash 不应相同"

    print("== hash 可复现 ==")
    ts2 = tiers(task)
    assert [t["args_hash"] for t in ts2] == [t["args_hash"] for t in ts], "同 seed 两次 hash 必须一致"
    print("  ok: 两次一致")

    print("== gen 档结构 ==")
    body = ts[1]["test"]
    assert "test_generated_test_cases" in body and "](200)" in body
    assert "_gen_cache" in body and "solution = Solution()" in body
    print("  ok: 隔离执行 + 缓存 + 候选 Solution")

    print("== humaneval 只有一档 ==")
    hv = {"task_id": "HumanEval/31", "dataset": "humaneval", "kind": "humaneval",
          "test": "def check(candidate):\n    pass\n", "code": ""}
    assert len(tiers(hv)) == 1
    print("  ok")

    print("\nALL INPUTS SELFTESTS PASSED")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="输入阶梯：native / gen200 / gen1000")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--task", default="3", help="EffiBench problem_idx")
    ap.add_argument("--show", default="t2", help="打印哪一档的测试代码前 400 字符")
    args = ap.parse_args(argv)
    if args.selftest:
        return _selftest()

    data = json.loads((ROOT / "datasets" / "dataset.json").read_text(encoding="utf-8"))
    raw = next(x for x in data if str(x["problem_idx"]) == str(args.task))
    task = {"task_id": "EffiBench/%s" % args.task, "dataset": "effibench",
            "kind": "test_block", "entry_point": "solution",
            "code": raw["canonical_solution"], "test": raw["small_test_cases"],
            "generator": raw["test_case_generator"]}
    for t in tiers(task):
        print("%-4s n=%s hash=%s" % (t["tier"], t["n"], t["args_hash"]))
    print("\n--- %s 测试代码前 400 字符 ---" % args.show)
    print(build(task, args.show)[:400])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

