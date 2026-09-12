---
name: conversation-memory
description: Resolve references to the current article and current attachment using persistent session memory.
---

# 会话记忆

每次消息先读取服务端注入的本会话受控上下文快照；快照已包含当前草稿、附件、摘要和最近消息。只有快照不足以判断“这篇文章”“上一条”“当前图片”时，才调用 `get_current_conversation_context`。这样普通消息不会因无关 Tool 循环而重复请求模型。

只有用户明确选择文章或 Tool 返回唯一对象后，才能调用 `set_current_draft`。

禁止从全局草稿列表默认选择第一篇；多个候选时必须请用户选择。记忆只保存草稿 ID、附件 ID 与简短事实摘要，不保存密钥、原始推理或外部网页正文。
