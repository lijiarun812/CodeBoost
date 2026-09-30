# CodeBoost

## 项目简介
本项目研究大模型生成代码的运行效率提升问题：在保持功能正确的前提下，如何降低生成代码的运行时间或内存占用。

## 参考论文
- PerfCodeGen (FORGE 2025)：主选方法，先用执行反馈修正功能，再用执行反馈改进运行效率。
- EffiLearner (NeurIPS 2024)：对照基线，将时间/内存剖析反馈给模型进行优化。
- EffiBench (NeurIPS 2024)：效率评测基准，提供 1000 道效率关键题和 6 个效率指标。

## 小组成员与分工
- 组长：李佳润，负责整体进度、论文主线梳理、最终汇总
- 成员A：PerfCodeGen 路线，环境搭建、基线复现
- 成员B：PerfCodeGen 路线，反馈实验、结果分析
- 成员C：EffiLearner 路线，剖析反馈、时间内存指标
- 成员D：EffiLearner 路线，对照分析、数据整理

## 目录结构
- `perfcodegen/`：PerfCodeGen 复现代码
- `effilearner/`：EffiLearner 复现代码
- `eval/`：效率评测脚本
- `results/`：实验数据与结果
- `docs/`：实验设计、进度记录

## 运行说明
待环境搭建完成后补充。
