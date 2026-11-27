# 第三方代码声明（NOTICE）

本目录下的以下文件来自 **PerfCodeGen 官方仓库**，仅用于课程复现对照，未做算法修改：

| 文件 | 说明 |
|---|---|
| `sanitize.py` | 官方代码清洗/抽取（`sanitize()` 被 `perfcodegen/pcg_repro.py` 直接调用） |
| `prompt.py` | 官方提示词模板加载器（保留备查；驱动里未直接使用） |
| `templates_chat.json` | 官方 chat 版提示词模板（论文 §2.1/§2.2 的 instruction 原文，**我们直接使用其中的模板文本**） |
| `fixed_examples.json` | 官方 few-shot 示例（保留备查） |

- 上游：https://github.com/SalesforceAIResearch/perfcodegen
- 取用版本：`2adc672`（2024 年快照）
- 许可：**Apache License 2.0**，Copyright (c) 2023 Salesforce, Inc.

```text
Licensed under the Apache License, Version 2.0 (the "License");
you may not use these files except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0
```

`perfcodegen/pcg_repro.py`、`eval/` 下的全部代码为本课程项目自写，见仓库根目录 `LICENSE`。
