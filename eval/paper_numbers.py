"""论文数值的唯一数据源（供 eval/make_table.py 引用）。

规矩：
1. 每个数字都要能指回 PDF 的具体位置，答辩被问“这个数哪来的”时能当场翻页；
2. 只收录读得清楚、且与论文正文互相印证的数；模糊的一律不写；
3. 括号里的百分比是论文自己给的下降比例，不要二次换算。
"""

EFFILEARNER = {
    "paper": "EffiLearner: Enhancing Efficiency of Generated Code via Self-Optimization（NeurIPS 2024）",
    "source": "doc/EffiLearner.pdf：Table 1（第 6 页）+ §4.3 Main Results 正文",
    "metric_units": {
        "ET": "执行时间 (s)",
        "MU": "最大内存占用 (MB)",
        "TMU": "总内存占用，内存-时间积分 (MB*s)",
    },
    # 每个指标：[初始值, EffiLearner 优化后, 论文给出的下降比例]
    "models": {
        "StarCoder2-15B": {
            "ET": [0.93, 0.12, "-87.1%"],
            "TMU": [22.02, 2.03, "-90.8%"],
        },
        "GPT-3.5-Turbo-0301": {
            "ET": [0.36, 0.28, "-22.2%"],
            "MU": [91.25, 36.08, "-60.5%"],
            "TMU": [157.50, 12.43, "-92.1%"],
        },
        "GPT-4": {
            "ET": [0.31, 0.28, "-9.7%"],
            "MU": [80.88, 63.82, "-21.1%"],
        },
        "DeepSeek-6.7B-Instruct": {
            "ET": [0.37, 0.34, "-8.1%"],
            "TMU": [555.18, 13.66, "-97.5%"],
        },
    },
    "note": "论文在 EffiBench 上跑；只统计通过全部测试用例的代码，因此成功率不影响“效率”指标。",
}

PERFCODEGEN = {
    "paper": "PerfCodeGen: Improving Performance of LLM Generated Code with Execution Feedback（FORGE 2025 @ ICSE）",
    "source": "doc/PerfCodeGen.pdf 摘要（第 1 页）+ 官方仓库 README「Key result」",
    # GPT-4 上“被优化到优于 ground truth”的题目比例
    "gpt4_opt_humaneval_pct": 47,
    "gpt4_opt_mbpp_pct": 56,
    "note": "论文主实验是 20 个采样 × 多轮自精炼，Best@k 与 %Opt 都有报告；"
            "MBPP 上 ~56% 的数字来自官方 README 的 Key result 段，与论文摘要一致。",
}

# 我们与论文的差异（汇报第 4 部分必须写清楚，这一列比数字本身占分）
DIFFS = [
    ("模型", "GPT-4 / StarCoder2 / DeepSeek-6.7B 等 22 个", "deepseek-flash、deepseek-v4-pro、glm-4.6、qwen3-coder-flash"),
    ("题目集", "EffiBench（1000 题）/ HumanEval / MBPP", "EffiBench 前 100 题（class Solution 题型）"),
    ("采样数", "PerfCodeGen sample_num=20；EffiLearner 单样本迭代", "base 采样默认 1（可调 --samples），另给 Best@k"),
    ("执行环境", "集群多 CPU  pod + 并行评测 + 官方 testcase 输入", "单机子进程沙盒；用例按 `def check(candidate)` 的 assert 切分"),
    ("时间测量", "单次 wall clock（集群）", "重复执行放大到 ~0.25s + 多轮取最小值 + 报告 MAD"),
    ("内存测量", "官方 profiler 的内存-时间积分（MB*s）", "同时给积分与归一化等效常驻内存，并剔除零积分题"),
    ("优化采纳判据", "低于原版本即采纳（按论文提示词）", "Pareto：时间至少快 5%，且内存不劣化 >5%"),
]
