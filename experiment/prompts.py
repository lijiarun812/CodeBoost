"""
4 组实验的 prompt 模板。
对齐大作业 docx 的方法表：
  Base          : 无优化（只让 LLM 按 HumanEval prompt 写函数）
  Vanilla       : 只说"请优化代码"，不给任何执行反馈
  PerfCodeGen   : 告诉它"最慢的输入是哪个、耗时多少"（test-case 级反馈）
  Ours(改进版)  : PerfCodeGen + 总时间 + 输入规模 + line_profiler 热点行
"""
import re

# ---------- Base：让 LLM 按 HumanEval prompt 补全函数 ----------
BASE_SUFFIX = """
Please complete the function above. Return ONLY the function implementation
(including the `def` line). Do not include tests, examples, or explanations.
Wrap your answer in a ```python ... ``` block.
"""


def base_prompt(task_prompt: str) -> str:
    return task_prompt + "\n" + BASE_SUFFIX


def correctness_prompt(task_prompt: str, code: str, error: str) -> str:
    return ("Fix the Python function so it satisfies the task and its tests. "
            "Return only the complete function in a ```python block.\n\n"
            f"Task:\n{task_prompt}\n\nCurrent code:\n{_wrap_code(code)}\n\n"
            f"Execution failure:\n{error[:1200]}")


# ---------- 优化阶段共用：把生成的代码包成块 ----------
def _wrap_code(code: str) -> str:
    match = re.search(r"```(?:python)?\s*\n(.*?)```", code, re.S | re.I)
    return f"```python\n{(match.group(1) if match else code).strip()}\n```"


# ---------- Vanilla：无反馈的裸优化 ----------
VANILLA_PROMPT = """The Python function below solves a programming task, but it may be inefficient.
Please rewrite it to be as fast as possible while keeping it correct on the given task.

Task signature and docstring:
{task_prompt}

Current implementation:
{code_block}

Rules:
- Return ONLY the rewritten function inside a ```python ... ``` block.
- Do not change the function name or signature.
- Do not include any test code or explanation.
- Preserve correctness; do not change the output for any valid input.
"""


def vanilla_prompt(task_prompt: str, code: str) -> str:
    return VANILLA_PROMPT.format(task_prompt=task_prompt, code_block=_wrap_code(code))


# ---------- PerfCodeGen：只告诉它最慢的输入和耗时 ----------
PERFCODEGEN_PROMPT = """The Python function below solves a programming task, but it may be inefficient.
We executed it on several test inputs. Here is the SLOWEST one:

Slowest input: {slowest_desc}
Time on that input: {slowest_ms:.1f} ms

Task signature and docstring:
{task_prompt}

Current implementation:
{code_block}

Please rewrite the function to be as fast as possible, especially on the slowest input above.
Rules:
- Return ONLY the rewritten function inside a ```python ... ``` block.
- Do not change the function name or signature.
- Do not include any test code or explanation.
- Preserve correctness; do not change the output for any valid input.
"""


def perfcodegen_prompt(task_prompt: str, code: str, slowest_desc: str, slowest_ms: float) -> str:
    return PERFCODEGEN_PROMPT.format(
        task_prompt=task_prompt, code_block=_wrap_code(code),
        slowest_desc=slowest_desc, slowest_ms=slowest_ms,
    )


# ---------- Ours：PerfCodeGen + 总时间 + 输入规模 + 热点行 ----------
OURS_PROMPT = """The Python function below solves a programming task, but it may be inefficient.
We profiled it across several inputs.

Execution summary:
- Slowest input: {slowest_desc}
- Time on slowest input: {slowest_ms:.1f} ms
- Sum of time across all inputs: {total_ms:.1f} ms
- Line-level hot spots on the largest input (sorted by total time, top {n_hot}):
{hot_lines_block}

Task signature and docstring:
{task_prompt}

Current implementation:
{code_block}

Please rewrite the function to be as fast as possible. Use the hot-spot information:
the lines listed above consume most of the time and are where algorithmic changes will pay off.
Rules:
- Return ONLY the rewritten function inside a ```python ... ``` block.
- Do not change the function name or signature.
- Do not include any test code or explanation.
- Preserve correctness; do not change the output for any valid input.
"""


def ours_prompt(task_prompt: str, code: str, times: list, profile: list) -> str:
    # times: [{"desc","median_ms"}, ...]
    # profile: [{"lineno","func","hits","total_ms"}, ...]
    slowest = max((t for t in times if "median_ms" in t), key=lambda x: x["median_ms"])
    total = sum(t.get("median_ms", 0) for t in times)
    hot_lines_block = "\n".join(
        f"  - line {p['lineno']} in {p['func']}: {p.get('source', '(source unavailable)')} "
        f"-- {p['total_ms']} ms over {p['hits']} hits"
        for p in profile[:5]
    ) or "  (no line-level profile available)"
    return OURS_PROMPT.format(
        task_prompt=task_prompt, code_block=_wrap_code(code),
        slowest_desc=slowest["desc"], slowest_ms=slowest["median_ms"],
        total_ms=total, n_hot=min(5, len(profile)),
        hot_lines_block=hot_lines_block,
    )
