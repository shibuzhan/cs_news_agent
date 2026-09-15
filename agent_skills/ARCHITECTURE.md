# Agent 能力编排边界

本目录的 `SKILL.md` 是给受控会话 DeepAgent 阅读的操作规范；Skill 本身不携带网络、文件系统或发布权限。

| Skill | 运行代码 | 可做的事 | 明确禁止 |
| --- | --- | --- | --- |
| `conversation-memory` | `app/agent_tools/conversation_context.py` | 读取本会话当前草稿、附件和摘要 | 跨会话猜测草稿、读数据库其他记录 |
| `draft-targeting` | `app/agent_tools/conversation_context.py` | 在用户明确选择后设置当前可编辑草稿 | 修改正文、审核或发布状态 |
| `illustration-workflow` | `app/tools/illustration_planner.py`、`app/tools/image_generation.py` | 规划插图位置、提交独立图片任务、绑定草稿插图 | 同步等待图片、写入正文、上传公众号 |
| `controlled-operations` | `app/tools/plan_tools.py` | 创建待确认的发布或定时计划 | 自动确认、注册任务、真实发表 |
| `arxiv-content-writing` | `app/services/generator.py` | 按论文证据生成研究解读 | 把论文结论写成落地事实 |
| `github-content-writing` | `app/services/generator.py` | 按 README 与受控补充证据生成项目解读 | 省略项目名、编造安装或功能 |
| `hacker-news-content-writing` | `app/services/generator.py` | 区分讨论观点与原始来源事实 | 将评论猜测当作事实 |
| `rss-content-writing` | `app/services/generator.py` | 按官方公告生成更新解读 | 编造版本、价格或部署步骤 |

项目文件和脚本能力是应用内部受控 Tool，而非对话 DeepAgent 的默认能力：

- `app/tools/files/workspace.py`：仅处理聊天附件的私有工作区副本。
- `app/tools/scripts/registry.py`：仅运行注册表中的固定脚本并限制时长。
- `app/tools/wechat_official_account.py`：仅发布页面和自动投递流程调用本地微信公众号官方 API Skill；不连接 MCP/SSE，也不提供发表能力。
- Exa 搜索适配层仅由采集/生成流程调用；会话 DeepAgent 不直接获得外网搜索、任意 URL 或账号权限。

四个来源写作 Skill 会在正文生成时按 `source_kind` 读取入口 `SKILL.md` 及其明确链接的 `references/evidence-and-search.md`，再加入受控提示词；它们也随 `agent_skills/` 挂载给会话 DeepAgent，用于保持来源边界的一致表述。生成器仍由服务端执行，Skill 不授予模型任何网络或发布权限。

每个来源 Skill 另有 `references/image-brief.md`，由 `app/services/image_brief.py` 在**配图任务**中按 `source_kind` 读取：`## 风格池` 提供彼此可替换的完整视觉方案（介质、光线、材质、镜头、色调），`## 实物池` 提供候选主体（科技、开源、论文与工程场景的实物）。风格按文章取一条（同一篇共用），实物按每张图取一个；两者都只能取**池内编号**——正文插图由规划模型按文章内容挑，越界或模型不可用时按草稿 ID 哈希轮换，封面直接用哈希，因此模型无法自创池外内容。服务端只追加构图约束、正向允许清单与语气参考，且不逐条列举"要避免的风格元素"——实测那种负向列举会让模型反而画出它们（详见 `findings.md`），只保留"画面内不出现中文/CJK 字形、品牌标识与人物"这一条负向约束（拉丁字母与数字可接受）。资源目录由 `app/paths.py` 解析，容器与本地一致。

这些 Skill 不设 `scripts/`：正文形态、字数区间（取自配置页）、GitHub 标题兜底和结构化输出由 `app/services/` 中的同一份服务端规则执行。为 Skill 再复制可执行校验会造成两套规则漂移；如未来出现可复用且不依赖业务运行时的辅助动作，再新增受测试覆盖的脚本。

`app/agents/content_deep_agent.py` 以会话 ID 复用 PostgreSQL checkpoint，在每条普通消息开始时恢复同一会话级 DeepAgent。它先接收受控上下文快照，再以 `ConversationDecision` 结构化结果交给路由层执行白名单流程；普通消息不会再先经过第二个独立意图模型。它仅暴露 `get_current_conversation_context`、`list_editable_drafts` 和 `set_current_draft` 三个业务 Tool，关闭默认子代理、文件读写、任意检索和执行脚本能力。
