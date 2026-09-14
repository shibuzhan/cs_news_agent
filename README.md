# 资讯运营 Agent

## 公众号草稿箱投递（本地官方 API Skill）

填写本地 `.env` 中的 `WECHAT_APP_ID` 与 `WECHAT_APP_SECRET` 后，应用会执行项目内 `agent_skills/wechat-official-account/scripts/wechat_official_api.py` 对应的本地 Tool，直接调用微信官方 REST API。该路径不启动、不连接 MCP/SSE，并显式不继承通用出站代理。

在“生成记录”页选中已审核通过的文案后按固定顺序操作：由素材规划模型从该文案已有图片中决定封面与正文插图 → 上传公众号素材 → 创建公众号草稿箱。“发布情况”页负责运行和查看自动审核、显示投递状态，并在投递失败时重新投递。当前个人账号流程以草稿箱创建为最终节点，不提供提交发布或已发表文章读取；图片、草稿 ID 和状态只保存审计字段，不保存公众号 AppSecret 或访问令牌。

聊天上传的图片默认仅保存在私有 MinIO，不会自动被模型读取、分析、上传到公众号或插入文案。发布素材库与聊天附件分离：聊天历史图片不会自动迁移或删除。发布素材仅在被模型选为封面或正文插图后，后端才读取并通过本地官方 API Tool 上传。审核文案在数据库、编辑区和预览区均为纯文本；为符合公众号接口，适配器仅在发送边界进行安全的段落和图片标签包装。公众号草稿箱创建成功后，系统将本地草稿标记为 `draftbox_created` 并进入来源去重审计。

第一版从 arXiv、GitHub Trending、Hacker News 和配置的官方 RSS 采集科技资讯，生成保留原文链接的中文待审核草稿。除已确认接入的微信公众号受控流程外，其他发布平台暂未绑定；系统不会自动发表。自动创建草稿只在同时启用自动审核与 `AUTO_WECHAT_DRAFT_ENABLED=true` 时发生，终点仍是公众号草稿箱。

## 已实现能力

- arXiv `cs.AI`、`cs.CL`、`cs.LG` 最新论文采集。
- GitHub 全站 daily/weekly Trending，各周期最多 25 项并合并重复仓库；每次只挑选一个未介绍项目读取完整 README 并生成一篇草稿。
- 也可**点名具体项目**：消息里写 `owner/repo` 或仓库链接（例如“affaan-m/ECC 获取这个项目生成文案”）时，只抓该仓库的元数据与完整 README，不读榜单、不参与热度排序；README 抓取失败则拒绝生成并给出原因。
- **界面按钮就是给 Agent 的命令**：运行自动审核、重写文案、审核通过/废弃/撤销、生成配图等按钮都会向会话 Agent 发送一条明确命令并在对话里留下消息；执行由 Agent 调用受控工具完成（长任务入队后台），结果由模型按真实结果汇报并按需追问。
- Hacker News Top Stories 采集，并在安全的公开 HTTP(S) 链接上提取正文；正文抓取失败保留 HN 原始记录和失败状态。
- 可配置官方 RSS 采集；默认使用 GitHub Changelog 官方 RSS。
- HTML 清洗、精确内容去重、主题分类、热度计算。
- OpenAI 兼容文案生成；只有显式设置 `LLM_ENABLED=true` 才会调用外部模型，默认使用确定性本地生成器。LLM 草稿按受限证据摘录改写，目标正文长度由 `DRAFT_BODY_MIN_CHARS`／`DRAFT_BODY_MAX_CHARS` 控制（当前 1200–4000 字，目标区间 1600–2200）。
- LangGraph 显式处理“标准化 → 信息量筛选 → 分类/排序 → 草稿生成”。
- 受控主 Agent 使用 LangGraph 编排四个内部采集 Tool；仅接受枚举来源和受限采集条数，不使用 LLM 自主选择工具。
- PostgreSQL 持久化、人工编辑、批准/驳回和幂等审核记录。
- 每个来源独立记录成功、部分成功或失败，不因单个来源不可用而中断整批采集。
- 保存 GitHub daily/weekly Trending 的排名与周期增星快照。
- `publication_records` 保存运营人员实际发布后的平台和链接；只有发布回填成功的 GitHub 项目才写入 `project_introductions`，下一次采集会在 README Tool 前排除它们。
- React 运营台：对话附件上传、受控处理提示和人工审核编辑页面。
- 私有 MinIO 文本附件：仅允许 `.txt`、`.md`、`.csv`，由后端签发短时下载链接。
- 通用对话与受控意图识别：支持普通对话、资讯采集、待确认定时计划和待确认发布计划。
- 每次对话 Agent 执行保存可展开的过程摘要，包括识别意图、已调用 Tool、来源和结果；不展示模型原始推理。
- 受控 AI 配图：由配图规划 Tool 决定 0–3 个正文段落位置，每张图一个独立后台任务，保存前经本地 OCR 质量门（只拦中文/CJK 字形，拉丁字母与数字可接受）；图片只进私有素材库，任一图片失败或超时会保留文字草稿并中断自动审核与投递。插图只能出现在正文开头或段与段之间，文末不插图。
- 按来源的配图风格：四个来源写作 Skill 各自提供 `references/image-brief.md`，含可替换的 `## 风格池`（介质、光线、材质、镜头、色调）与 `## 实物池`（科技／开源／论文／工程场景实物）。风格按文章取一条、实物按每张图取一个，两者都只能取池内编号（规划模型按内容挑，越界或不可用时按草稿 ID 哈希轮换），因此同一来源的不同文章会换风格与实物。提示词不逐条列举"要避免的风格元素"——实测那样会让模型反而画出全息面板、电路板与大脑等元素（见 `findings.md`）。
- **长期排版偏好（可对话修改、落库持久）**：封面图同时作为正文首图；固定结尾图取代文末文字尾注；文字尾注开关。存于 `app_settings`，重建容器不丢。
- **长文写作偏好用仓库文件**：`preferences/style.md`（挂载进容器）在生成/改稿/审核三处提示词注入；Agent 可用 `update_style_guide` 读写，改动在宿主机留下 Git diff；只有注释时自动忽略。
- **投递选图接入视觉模型**：对来源真实截图传图识别内容后决定封面与正文插图；AI 配图仍只用文字描述。口径为“真实截图优先，缺口用 AI 配图补足”，并有确定性策略保证真实截图入选、同一段落只放一张图。
- **来源真实截图**：从仓库 README／官方页面提取图片（含 HTML `<img>`／`<picture>`），经魔数、体积校验后存入私有素材库并可绑定为封面或正文插图；封面未变化时复用已上传的永久素材 `media_id`，避免素材库堆积。
- **公众号草稿留言开关**：创建与覆盖草稿时显式下发 `need_open_comment`／`only_fans_can_comment`。
- **素材库工具**：`list_wechat_materials`（只读盘点可清理项）与 `delete_wechat_material`（删除永久素材，仍被投递记录引用的封面会被拒绝）。
- **完成后追加新消息**：任务结束以**新消息**汇报，占位消息（“正在生成…”）保留，历史不被覆盖。
- **入队即回执**：发消息后立即收到确定性回执（不调用模型），多步指令（“先配图，然后自动审核”）会按顺序规划并逐步执行；同一条消息只对应一条运行卡片，状态用短状态刷新。
- **对话回复与任务汇报支持 SSE 流式**：增量逐字渲染，失败或断开自动回退非流式/轮询；结构化路径保持非流式。
- **审核等待配图**：配图未完成时不立即发起审核，由配图收束逻辑自动接续，避免“审核早于配图”。
- **生成记录状态块按真实数据**：文字依据草稿是否存在（`文案已生成（版本 N）`），配图依据图片任务真实状态（`配图已完成（N 张）`等）。
- **GitHub API 令牌**：配置 `GITHUB_TOKEN` 后核心 API 配额由 60 次/小时提升到 5000 次/小时。
- 自动审核与草稿箱投递：固定规则先行，模型一次返回评分、严重度与完整问题清单；达到阈值且无 critical 问题时最多自动改稿一轮，通过后最多创建公众号草稿箱条目。

## Docker 启动

1. 可选：复制 `.env.example` 为 `.env` 并修改开发密码、RSS 和模型配置。
2. 构建并启动：

   ```powershell
   docker compose up -d --build
   ```

3. 检查状态：

   ```powershell
   docker compose ps
   Invoke-RestMethod http://127.0.0.1:8000/api/health
   ```

接口文档位于 `http://127.0.0.1:8000/docs`。PostgreSQL、应用、MinIO API、MinIO 控制台与前端端口都仅绑定本机地址；数据分别保存在 `postgres_data` 和 `minio_data` 命名卷中。前端运营台为 `http://127.0.0.1:5173`，MinIO 控制台为 `http://127.0.0.1:9001`。

## 主要接口

- `POST /api/collections/all?limit=25`：采集所有已配置来源。
- `POST /api/collections/github?limit=25`：采集 GitHub Trending。
- `POST /api/agent/collect`：通过主 Agent 采集指定来源；不传 `sources` 时采集全部已配置来源。
- `GET /api/agent/runs`：查看主 Agent 执行记录及其 Tool 汇总结果。
- `GET /api/collections/runs`：查看逐来源采集运行记录和错误。
- `GET /api/sources/health`：查看四类来源最近状态、最近成功时间和过期标记。
- `GET /api/sources`：查看标准化资讯和热点分数。
- `GET /api/sources/{source_item_id}/trends`：查看 GitHub 项目的 Trending 历史快照。
- `GET /api/drafts?status=pending_review`：查看待审核草稿。
- `PATCH /api/drafts/{draft_id}`：编辑正文、标题、标签、卡片脚本或分类。
- `POST /api/drafts/{draft_id}/review`：批准或驳回草稿。
- `POST /api/chat/sessions`：创建运营对话。
- `POST /api/chat/sessions/{session_id}/attachments`：私有保存文本附件；不会读取附件或调用 LLM。
- `POST /api/chat/sessions/{session_id}/messages`：发送对话消息。**写入用户消息与一条确定性回执后立即返回**（不调用模型、不入库等待执行结果），真正的意图识别与工具执行在 Worker 中完成。
- `GET /api/chat/agent-runs/{run_id}/stream`：SSE 事件流（`delta` 增量文本、`milestone` 里程碑、`done` 完整文本与状态）。Redis 不可用或客户端断开时自动退化为按数据库轮询，最终仍下发完整文本。
- `GET /api/attachments/{attachment_id}/download?token=...`：通过短时签名链接下载附件。
- `POST /api/schedule-plans/{plan_id}/confirm`：确认已创建的定时计划记录；当前不会注册或执行任务。
- `POST /api/publish-plans/{plan_id}/confirm`：确认已创建的发布计划记录；当前不会连接账号或发布内容。

审核请求示例：

```json
{
  "reviewer": "operator-01",
  "action": "approve",
  "note": "已核对原文",
  "idempotency_key": "review-20260905-0001"
}
```

批准后草稿状态变为 `ready_to_publish`，只表示可以人工复制发布，不会调用任何发布平台。

## 主 Agent 与内部 Tool

主 Agent 当前只支持结构化采集指令，来源只能是 `arxiv`、`github`、`hacker_news`、`rss`，不能输入任意网址、工具名或数据库语句。每个来源对应一个内部 Tool；单个 Tool 失败会记录为部分完成，不会阻断其他来源。

```json
{
  "action": "collect",
  "sources": ["arxiv", "hacker_news"],
  "limit": 10
}
```

调用示例：

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8000/api/agent/collect `
  -ContentType 'application/json' `
  -Body '{"action":"collect","sources":["arxiv"],"limit":2}'
```

`agent_runs` 保存一次主 Agent 执行的父级审计记录，`collection_runs` 保存每个来源的子级运行记录。当前 Tool 仅在项目内部使用，并未提供 MCP Server；未来如需外部 Agent 客户端接入，可基于 `app/tools/` 增加适配层。

## 本地离线测试

```powershell
uv sync --extra dev
.\.venv\Scripts\python.exe -m pytest
```

测试使用 `tests/fixtures/` 中的固定响应，不访问 arXiv、GitHub、Hacker News、RSS 或 LLM。

如果 app/worker 容器正在运行，`runtime_logs/news-agent.log` 会被容器占用，导入 `app.worker` 时会因无法写日志而中断收集。此时把日志路径指向别处再运行：

```powershell
$env:LOG_FILE = Join-Path $env:TEMP "news-agent-pytest.log"
.\.venv\Scripts\python.exe -m pytest
```

少数用例需要 pytest 临时目录；若本机沙箱限制临时目录写入，可用 `--deselect` 跳过对应用例，其余用例不受影响。

## 数据库与迁移

审核通过只表示内容可发布，不会影响 GitHub 选题去重。运营人员完成外部平台发布后，应在审核页填写平台名称和实际发布链接；系统才会将草稿标为“已发布”，并为 GitHub 项目写入介绍历史。本项目不会登录任何平台、调用平台 API 或自动发布。

容器启动时自动执行：

```powershell
alembic upgrade head
```

本地演示数据可在容器启动后写入：

```powershell
docker compose exec app python scripts/demo.py
```

数据库凭据仅从环境变量读取。`.env` 已被忽略，不要把真实密钥或密码提交到代码库。

容器镜像使用 `requirements.txt` 缓存运行依赖层；它与 `pyproject.toml` 的运行依赖保持一致。修改依赖时需同时更新二者并重新构建。

## 外部模型安全开关

模型地址、密钥和模型名存在并不等于启用外部调用。只有同时配置以下值，系统才会调用 OpenAI 兼容接口：

```dotenv
LLM_ENABLED=true
OPENAI_API_KEY=your-secret
LLM_MODEL=your-model
```

> 各任务的接口地址、密钥与模型名请在 `.env` 中配置（键名见 `.env.example`）。README 不列出任何具体服务地址。

开发和真实来源采集验收建议保持 `LLM_ENABLED=false`，先确认待处理数量，再单独进行小批量模型验收。

### 结构化输出模式

文案生成、自动审核和会话决策都要求模型返回受 Pydantic 校验的结构化结果，有两种模式：

```dotenv
# tool（默认）：强制调用结构化工具，适用于普通模型。
# json：只要求 JSON 文本，由服务端本地 Pydantic 校验，不发送 tool_choice。
LLM_STRUCTURED_OUTPUT_MODE=tool
CONVERSATION_STRUCTURED_OUTPUT_MODE=
CONTENT_STRUCTURED_OUTPUT_MODE=
REVIEW_STRUCTURED_OUTPUT_MODE=
```

思考模式模型会以 HTTP 400 拒绝 `tool_choice=required`（或具体工具对象）。此时将对应任务设为 `json` 即可；留空时回退 `LLM_STRUCTURED_OUTPUT_MODE`，再回退 `tool`，因此旧配置行为不变。两种模式都保持严格校验：模型输出不是可解析 JSON、不是对象或不符合 Schema 时明确报错，不会降级生成替代内容，也不会执行任何业务操作。

### 失败原因与脱敏

失败任务在生成记录列表中只显示“任务失败 + 简短原因”，不会出现供应商原始响应、请求 ID 或密钥。原因由 `app/services/model_errors.py` 统一分类，只依据异常类型与结构化的 `status_code`/`code`/`param` 判定，例如：

- 思考模式拒绝强制工具调用 → 提示把该任务的结构化输出模式改为 `json`；
- 402 额度不足、401/403 凭据或权限、404 模型名、408/超时、429 限流、5xx 服务端异常、连接失败；
- 结构校验失败 → “内容模型输出不符合草稿结构”。

该分类同时作用于任务摘要、`collection_runs` 与聊天事件审计，并对历史文本做兜底清洗。历史遗留的长错误文本可用 `scripts/cleanup_raw_error_text.py` 就地清理（默认仅预览，`--apply` 才写回，只改文本不删除记录）。

## 对话附件与审核运营台

前端采用 React + Vite + TypeScript + Tailwind CSS 的 shadcn/ui 风格组件，提供“与 Agent 对话”和“人工审核”两个页面。首版对话附件只允许 `.txt`、`.md`、`.csv`，最大大小由 `ATTACHMENT_MAX_BYTES` 控制（默认 2 MB）。附件保存到私有 MinIO 桶，数据库仅保存元数据、哈希和对象键；下载链接由后端签发，默认 15 分钟过期。

上传、普通对话、选择附件都不会读取附件，也不会调用 LLM。只有同一对话中附带附件 ID 并明确输入“提取附件并生成待审核草稿”（或等效的“创建”表述）时，系统才允许调用固定的文本提取 Tool。即使指令明确，`LLM_ENABLED` 仍必须为 `true`；否则接口只返回“模型未启用”，附件保持未处理状态。

第一版没有账号体系，因此短时链接是当前的访问控制边界。上线前应补充登录鉴权、按用户/团队校验附件归属，并替换 `.env.example` 中的默认 MinIO 与短链签名密钥。

## 通用对话与计划 Tool

普通消息会先被识别为受限意图，再决定是否调用已登记 Tool：`general_chat`、`collect_news`、`create_schedule_plan`、`create_publish_plan` 和 `attachment_draft`。模型只返回经过 JSON 校验的意图和受限字段，不能选择任意工具、URL、数据库操作或发布接口。每次响应会保存“执行过程摘要”，前端可折叠展开查看已识别意图、调用的 Tool 与结果；该摘要不是模型原始思维链。

### 对话响应：入队即回执、同一条消息一条运行

`POST /api/chat/sessions/{session_id}/messages` 只做四件事：写用户消息、写一条**确定性回执**（`app/services/chat_receipts.py`，复用 `parse_agent_command()` 的分类，不调用模型）、建一条受理运行、入队 `process_general_chat_job`。因此响应时间与模型、图片生成无关（实测 30–60 ms）。

- 回执会先说明理解到的步骤：界面按钮命令 → 单步；自然语言里的多条指令（例如“帮我给这篇文章配图，然后自动审核”）→ 按出现顺序规划成多步并逐步执行；问句不会被误报成动作。
- 同一条用户消息只对应**一条运行**：Agent 工具通过 `chat_run_id` 复用它，运行卡片上的短状态（配图已入队／审核中／采集已入队…）与里程碑逐步刷新；结束时的汇报作为**新消息追加**，不覆盖受理回执。
- 对话页在存在运行中任务时每 2 秒轮询消息与运行事件，空闲时自动停止；同时按运行的 SSE 流实时渲染增量文本。遗留运行会在会话接口被顺带收束，不会永久显示“处理中”。
- **正文也是流式可见的**：长文生成是一次 9–12 分钟的模型调用，系统用同一套增量提取器把 `body` 字段边写边推到对话页（标记“正在写正文 · 已 N 字”，多来源采集换篇时自动清空上一段）。流式不可用时只影响可见性，不影响生成本身。
- 流式只用于“对话回复”“任务汇报”与“正文写作”三处。生成结果的结构校验、审核与改稿等**结构化输出**路径保持非流式：它们要求完整 JSON 才能通过 Pydantic 校验。
- **语气口径**：正文按“资讯分享者”写（第一人称判断克制使用、可以口语化），禁止百科定义句与研报腔；长期规范放在仓库文件 `preferences/style.md`，会同时注入生成/改稿/审核三处。
- **审核口径**：只审两件事——**语气**（是不是分享者在讲，而不是百科词条）与**通顺**（重复绕圈、指代不清、句子接不上）；术语选择、措辞偏好、要不要补背景等细节不再计入缺陷，minor 最多 2 条且只用于语气与通顺。

“每天 9 点采集 AI 资讯”会创建 `pending_confirmation` 定时计划；“发布到某平台”会创建 `pending_confirmation` 发布计划。确认按钮当前只把计划状态改为 `confirmed`，不注册调度器、不连接平台账号、不执行发布。计划 Tool 与规则 Skill 分别位于 `app/tools/plan_tools.py` 和 `agent_skills/controlled-operations/SKILL.md`。

## GitHub 连通性排查

GitHub Trending 依赖 `https://github.com/trending`。如果来源健康接口显示 GitHub 失败，先在主机和容器中检查 `github.com` 是否被解析到可连接的公网地址；不要在应用代码中绕过本机 DNS、代理或访问策略。

## 第一版明确不包含

- PDF、DOCX 等非文本附件的解析；附件仅支持 `.txt`、`.md`、`.csv`，图片仅支持 JPG/JPEG/PNG。
- 项目自身不提供 MCP Server；仅在启用 Exa 联网补充证据时作为客户端调用远程 Exa MCP。
- 真实定时任务执行；定时与发布计划只保存待确认记录，确认不等于执行。
- 微信公众号的提交发表、群发与已发表内容读取；当前账号流程的终点是创建或更新草稿箱条目。
- 除已确认的微信公众号受控草稿箱流程外，其他发布平台适配、账号登录或自动发布。
