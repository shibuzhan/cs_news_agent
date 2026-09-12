---
name: content-writing
description: Generate evidence-grounded technology content drafts only after a source evidence pack is available.
---

# 内容增强生成

只使用证据包的事实和证据 ID。先给出受众、角度和提纲，再生成草稿。关键结论必须引用证据 ID；风险或证据不足时标为人工审核，不得补充事实。

专有名词第一次出现时，用括号补充一句来源支持的通俗解释。来源没有解释、且该术语会显著影响读者理解时，才可提出最多两条检索词，由受控 `ExaMcpSearchTool` 执行；不得自行访问网页、调用未登记 Tool 或将搜索片段当作未经核验事实。

正文使用 4 到 8 个自然段连续叙述，自然覆盖背景、技术或过程、价值与边界、后续观察，但不输出这些名称、“先说结论”或编号。正文至少 1200 字符；每段开头由服务端统一添加两个全角空格。来源专用规则以 `agent_skills/` 下相应来源的写作 Skill 为准。正文不得包含原文链接，只由服务端在文末保留一次“原文标题：”。来源 URL 交由公众号草稿的 `contentSourceUrl` 字段作为“阅读原文”跳转。`summary_cn` 专供公众号 description 使用：单句、吸引点击、不夸张、不得换行，最长 120 个字符。
