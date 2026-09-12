---
name: file-operations
description: Handle only explicitly authorized text attachments in the current chat session.
---

# 会话文件操作

默认只保存附件。仅在用户明确要求读取、提取或转换时调用文件 Tool。只使用附件 ID，不接受磁盘路径；输出必须创建为新文件，不覆盖来源文件。
