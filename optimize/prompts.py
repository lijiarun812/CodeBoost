"""One fixed prompt frame for the five feedback arms."""

from __future__ import annotations

COMMON_REQUIREMENT = (
    "Optimize the code while preserving its behavior and public interface. "
    "Return only a complete Python program in a python code fence."
)

_FRAME = (
    "Task description:\n{description}\n\n"
    "Initial code:\n```python\n{code}\n```\n\n"
    "Optimization requirement:\n{requirement}\n\n"
    "Performance feedback:\n{feedback}\n"
)


def render_prompt(description: str, code: str, feedback: str) -> str:
    """Only feedback differs among F0–F4 for the same description and code."""
    return _FRAME.format(
        description=description,
        code=code,
        requirement=COMMON_REQUIREMENT,
        feedback=feedback,
    )
