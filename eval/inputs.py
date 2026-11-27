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
import ast
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


# ---------------------------------------------------------------------------
# 输入规模升档（按字面量放大实参）——**只用于空间诊断，绝不进时间主结果**
# ---------------------------------------------------------------------------
# 为什么另搞一套：上面的 tiers 只放大「用例条数」，单条用例的输入规模不变，
# 所以 peak_tracemalloc 还在亚 KB 级、peak_rss 还贴在解释器基线上（实测 40 题
# MU 可分辨 0 题）。要谈「省内存」，必须让**单次调用**真的分配内存。
#
# 三条硬约束（写死在代码里，防止以后被误用）：
# 1. **只做资源探针**：把 assert solution.f(x) == y 改成只调用 solution.f(x)，
#    丢掉比较——放大后的输入答案必然不同，判对错没有意义。
# 2. **只进 diagnostic_only**：放大输入改变了工作量，时间数字与主结果不可比。
# 3. **有上限**：单个参数最长 MAX_SEQ，防一次调用把机器打爆。
SCALE_FACTORS = (1, 10, 100, 1000)
MAX_SEQ = 1_000_000
MAX_INT = 10 ** 9
MAX_CALLS = 8


def _scalar_const(node):
    """字面量标量（含负数/带符号数）。负数在 AST 里是 UnaryOp(USub, Constant)。"""
    if isinstance(node, ast.Constant) and not isinstance(node.value, bool) and node.value is not None:
        return True
    return (isinstance(node, ast.UnaryOp)
            and isinstance(node.op, (ast.USub, ast.UAdd))
            and isinstance(node.operand, ast.Constant)
            and isinstance(node.operand.value, (int, float))
            and not isinstance(node.operand.value, bool))


def _scale_node(node, factor):
    """把字面量实参按 factor 放大；非字面量原样保留（不敢猜语义）。

    支持：字符串（拉长）、标量数字（含负数）、扁平 list/tuple（重复）、
    嵌套 list（重复外层；内层保持，避免 100x100 的双重爆炸）。
    注意：重复同一个容器的元素只是复制**引用**，对内存峰值的贡献主要来自容器本身；
    要真正长内存，靠的是把容器长度放大到 1e5–1e6 量级（指针数组本身就有几 MB）。
    """
    if _scalar_const(node):
        sign = 1
        val = node.value if isinstance(node, ast.Constant) else node.operand.value
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            sign = -1
        if isinstance(val, str):
            n = min(max(len(val), 1) * factor, MAX_SEQ)
            reps = n // max(len(val), 1) + 1
            return ast.Constant(value=(val * reps)[:n])
        if isinstance(val, int):
            return ast.Constant(value=min(abs(val) * factor, MAX_INT) * sign)
        return node
    if isinstance(node, (ast.List, ast.Tuple)):
        elts = list(node.elts)
        if not elts:
            return node
        n = min(len(elts) * factor, MAX_SEQ)
        reps = n // len(elts) + 1
        return type(node)(elts=(elts * reps)[:n], ctx=ast.Load())
    return node


_BUILDER = (
    "def _cb_list(n, kind=0):\n"
    "    if kind == 1:\n"
    "        return ['ab' * 3 for _ in range(n)]\n"
    "    return [i % 997 for i in range(n)]\n"
    "def _cb_str(n):\n"
    "    return ('abcdefgh' * (n // 8 + 1))[:n]\n"
    "def _cb_nested(rows, cols):\n"
    "    return [[i % 101 for _ in range(cols)] for i in range(rows)]\n"
)


def _builder_expr(node, factor):
    """把字面量实参换成「运行时构造同等规模序列」的表达式。

    为什么不展开成字面量：1e6 个元素的字面量会让源码涨到几 MB，光是 compile 就要几秒，
    测出来的时间/内存全被编译污染。改成 `_cb_list(1000000)` 这类构造调用：
    源码恒定，**运行时真的分配 n 个新对象**（这才是我们要测的那部分内存）。
    """
    if _scalar_const(node):
        val = node.value if isinstance(node, ast.Constant) else node.operand.value
        sign = -1 if (isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub)) else 1
        if isinstance(val, str):
            n = min(max(len(val), 1) * factor, MAX_SEQ)
            return ast.parse("_cb_str(%d)" % n, mode="eval").body
        if isinstance(val, int):
            return ast.Constant(value=min(abs(val) * factor, MAX_INT) * sign)
        return node
    if isinstance(node, (ast.List, ast.Tuple)):
        elts = list(node.elts)
        if not elts:
            return node
        if all(_scalar_const(e) for e in elts):
            n = min(len(elts) * factor, MAX_SEQ)
            first = elts[0]
            fval = (first.value if isinstance(first, ast.Constant)
                    else (first.operand.value if isinstance(first, ast.UnaryOp) else None))
            kind = 1 if isinstance(fval, str) else 0
            return ast.parse("_cb_list(%d, kind=%d)" % (n, kind), mode="eval").body
        if all(isinstance(e, (ast.List, ast.Tuple)) for e in elts):
            rows = min(len(elts) * factor, MAX_SEQ)
            cols = max(len(e.elts) for e in elts)
            return ast.parse("_cb_nested(%d, %d)" % (rows, cols), mode="eval").body
        return node
    return node


def scaled_calls(case_src, factor, max_calls=MAX_CALLS):
    """从测试块里抽出 `solution.<method>(...)`，把实参换成按 `factor` 放大的构造表达式。

    返回 (calls, note)：calls 是逐行调用源码（第一行会带上 `_cb_*` 构造器定义）。
    """
    if factor < 1:
        raise ValueError("factor must be >= 1")
    try:
        tree = ast.parse(case_src)
    except SyntaxError as exc:
        return [], "parse_error: %s" % exc
    calls = []
    for stmt in tree.body:
        target = stmt.test if isinstance(stmt, ast.Assert) else (stmt.value if isinstance(stmt, ast.Expr) else None)
        if target is None:
            continue
        picked = None
        for node in ast.walk(target):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == "solution"):
                picked = node
                break
        if picked is None:
            continue
        new_call = ast.Call(func=ast.parse(ast.unparse(picked.func), mode="eval").body,
                            args=[_builder_expr(a, factor) for a in picked.args],
                            keywords=[])
        try:
            calls.append(ast.unparse(ast.fix_missing_locations(ast.Expression(body=new_call))))
        except Exception:  # noqa: BLE001
            continue
    if not calls:
        return [], "no_solution_call"
    return [_BUILDER] + calls[:max_calls], "ok"


def scale_tier(task, factor):
    """构造「单条用例输入规模 x factor」的资源探针测试块（diagnostic_only）。"""
    case_src = task.get("test") or ""
    calls, note = scaled_calls(case_src, factor)
    body = "\n".join(calls)
    return {
        "factor": factor,
        "n_calls": max(0, len(calls) - 1) if note == "ok" else 0,
        "test": body,
        "note": note,
        "args_hash": _hash(body, factor),
        "diagnostic_only": True,
    }


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

