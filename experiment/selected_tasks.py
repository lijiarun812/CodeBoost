"""
选 12 道 HumanEval 题，每题配 small/medium/large 三档输入用于性能测试。
正确性测试仍用 HumanEval 自带 check()；计时用这里造的大输入。
"""
import json
import os
import random

HERE = os.path.dirname(os.path.abspath(__file__))


def _load_humaneval():
    rows = {}
    with open(os.path.join(HERE, "HumanEval.jsonl"), encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            rows[r["task_id"]] = r
    return rows


# (humaneval_index, 三档输入描述列表)
# 每档: (args_tuple, human_readable_description)
# list 类题用固定种子生成，保证 4 组方法跑完全一致的输入
SELECTED = [
    # ---- 整数输入类 ----
    (31, "is_prime", [
        ((101,),      "n = 101 (small)"),
        ((10007,),    "n = 10007 (medium)"),
        ((99999989,), "n = 99999989, a large prime near 1e8 (large)"),
    ]),
    (55, "fib", [
        ((10,),  "n = 10 (small)"),
        ((25,),  "n = 25 (medium)"),
        ((35,),  "n = 35, naive recursion is already slow here (large)"),
    ]),
    (59, "largest_prime_factor", [
        ((13195,),          "n = 13195 (small)"),
        ((600851475143,),   "n = 600851475143, the Project Euler #3 number (medium)"),
        ((1000000007,),     "n = 1000000007, a large prime (large)"),
    ]),
    (60, "sum_to_n", [
        ((100,),       "n = 100 (small)"),
        ((1_000_000,), "n = 10^6 (medium)"),
        ((10_000_000,), "n = 10^7 (large)"),
    ]),
    (63, "fibfib", [
        ((8,),  "n = 8 (small)"),
        ((15,), "n = 15 (medium)"),
        ((20,), "n = 20, naive recursion is exponential (large)"),
    ]),
    (96, "count_up_to", [
        ((100,),      "n = 100 (small)"),
        ((10_000,),   "n = 10^4 (medium)"),
        ((100_000,),  "n = 10^5 (large)"),
    ]),
    (123, "get_odd_collatz", [
        ((14,),      "n = 14 (small)"),
        ((1_000_000,), "n = 10^6 (medium)"),
        ((999_999,),    "n = 999999, a long Collatz chain (large)"),
    ]),
    (130, "tri", [
        ((5,),  "n = 5 (small)"),
        ((20,), "n = 20 (medium)"),
        ((50,), "n = 50 (large)"),
    ]),
    (139, "special_factorial", [
        ((4,),  "n = 4 (small)"),
        ((8,),  "n = 8 (medium)"),
        ((11,), "n = 11, product of 1!*2!*...*n! (large)"),
    ]),
    (147, "get_max_triples", [
        ((10,),   "n = 10 (small)"),
        ((200,), "n = 200 (medium)"),
        ((500,), "n = 500, naive O(n^3) triples loop (large)"),
    ]),
    # ---- 列表输入类（固定种子生成） ----
    (40, "triples_sum_to_zero", [
        (None, "list of 30 random ints in [-50, 50] (small)"),
        (None, "list of 300 random ints in [-500, 500] (medium)"),
        (None, "list of 2000 random ints in [-5000, 5000] (large)"),
    ]),
    (120, "maximum", [
        (None, "arr of 200 random ints, k=3 (small)"),
        (None, "arr of 20000 random ints, k=10 (medium)"),
        (None, "arr of 200000 random ints, k=100 (large)"),
    ]),
]


def _make_list_inputs():
    """为列表类题生成固定种子的输入。返回 {task_id: [(args, desc), ...]}"""
    rng = random.Random(20261023)
    out = {}
    # HumanEval/40 triples_sum_to_zero(l)
    out["HumanEval/40"] = [
        ((rng.choices(range(-50, 51), k=30),),
         "list of 30 random ints in [-50, 50] (small)"),
        ((rng.choices(range(-500, 501), k=300),),
         "list of 300 random ints in [-500, 500] (medium)"),
        ((rng.choices(range(-5000, 5001), k=2000),),
         "list of 2000 random ints in [-5000, 5000] (large)"),
    ]
    # HumanEval/120 maximum(arr, k) -> returns list of k largest sorted
    out["HumanEval/120"] = [
        ((rng.choices(range(-1000, 1001), k=200), 3),
         "arr of 200 random ints, k=3 (small)"),
        ((rng.choices(range(-10000, 10001), k=20000), 10),
         "arr of 20000 random ints, k=10 (medium)"),
        ((rng.choices(range(-100000, 100001), k=200000), 100),
         "arr of 200000 random ints, k=100 (large)"),
    ]
    return out


def build_tasks():
    he = _load_humaneval()
    list_inputs = _make_list_inputs()
    tasks = []
    for idx, ep, tiers in SELECTED:
        # find humaneval record by entry_point
        rec = next(r for r in he.values() if r["entry_point"] == ep)
        task_id = rec["task_id"]
        # fill tiers
        filled = []
        if task_id in list_inputs:
            for args, desc in list_inputs[task_id]:
                filled.append({"args": args, "desc": desc})
        else:
            for args, desc in tiers:
                filled.append({"args": args, "desc": desc})
        tasks.append({
            "task_id": task_id,
            "entry_point": ep,
            "prompt": rec["prompt"],
            "test": rec["test"],
            "tiers": filled,
        })
    return tasks


if __name__ == "__main__":
    ts = build_tasks()
    for t in ts:
        print(t["task_id"], t["entry_point"], "tiers:", len(t["tiers"]))
        for tier in t["tiers"]:
            print("   ", tier["desc"])
