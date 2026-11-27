# 旧口径 vs 新口径（同一份 final_code）

> 旧口径 = `final.exec_time / repeats`（中期 psutil 那套）；新口径 = `eval/harness.py` 的 E=12 去极值。代码完全相同，只有尺子不同。
> 结论只回答“**口径差多少**”，不重新解释方法效果。

| 模型 | 题目 | 重复次数 | 旧口径 ms | 新口径 ms | 新/旧 | status |
|---|---|---|---|---|---|---|
| deepseek-flash | EffiBench/3 Longest Substring Without  | 20000 | 0.0024 | 0.0040 | 1.67x | ok |
| deepseek-flash | EffiBench/4 Median of Two Sorted Array | 20000 | 0.0011 | 0.0022 | 2.04x | ok |
| deepseek-flash | EffiBench/10 Regular Expression Matchin | 20000 | 0.0026 | 0.0047 | 1.78x | ok |
| deepseek-v4-pro | EffiBench/3 Longest Substring Without  | 12038 | 0.0022 | 0.0039 | 1.74x | ok |
| deepseek-v4-pro | EffiBench/4 Median of Two Sorted Array | 20000 | 0.0010 | 0.0017 | 1.77x | ok |
| deepseek-v4-pro | EffiBench/10 Regular Expression Matchin | 20000 | 0.0030 | 0.0053 | 1.79x | ok |
| glm-4.6 | EffiBench/3 Longest Substring Without  | 20000 | 0.0022 | 0.0039 | 1.75x | ok |
| glm-4.6 | EffiBench/4 Median of Two Sorted Array | 20000 | 0.0014 | 0.0028 | 2.04x | ok |
| glm-4.6 | EffiBench/10 Regular Expression Matchin | 20000 | 0.0028 | 0.0049 | 1.72x | ok |
| qwen3-coder-flash | EffiBench/3 Longest Substring Without  | 20000 | 0.0024 | 0.0043 | 1.77x | ok |
| qwen3-coder-flash | EffiBench/4 Median of Two Sorted Array | 20000 | 0.0011 | 0.0022 | 1.98x | ok |
| qwen3-coder-flash | EffiBench/10 Regular Expression Matchin | 20000 | 0.0028 | 0.0051 | 1.85x | ok |

## 汇总

- 重测题数：**12**（missing=0，timeout=0）
- 新/旧 比值中位数：**1.78x**
- 新口径偏大：12 题；偏小：0 题

> 判读：比值系统性偏离 1 说明两把尺子的“绝对量级”不同（估计量 + 输入档不同）；真正要比方法效果时，必须在**同一把尺子**下比 baseline 与 candidate，不能跨口径比。
