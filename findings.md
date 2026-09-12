# 实施发现

## 2026-09-11｜Phase 53 异步审核与 GitHub 正文尾注

- 手动审核路由此前直接 `await auto_review_and_create_wechat_draft(...)`，一次请求会同步经过自动改稿、模型审核和公众号草稿箱投递。运行日志已记录单次请求约 458.8 秒。
- 审核与自动改稿均使用同步 OpenAI SDK；移动到 ARQ 后仍需通过线程池执行同步模型调用，避免阻塞 Worker 的事件循环。
- `WECHAT_INLINE_IMAGE_MAX_BYTES = 1 * 1024 * 1024` 只在正文插图上传前检查；封面上传路径没有该限制。该报错发生在调用公众号 MCP 前。
- `append_source_title` 在普通生成、确定性生成、自动改稿与重生成路径统一调用；审核规则要求恰好一个尾注。GitHub 取消尾注须同步所有入口和审核规则。
- 实施后 Worker 启动日志确认 `process_auto_review_job` 已注册；请求侧只负责创建 `queued` 记录和 ARQ 入队。模型调用在 `run_in_threadpool` 中进行，Worker 事件循环仍可调度其他 Job。

- 2026-09-10：原记录重生成会复用 `chat_agent_runs` 行，而遗留任务收束此前按该行首次 `created_at` 判断。旧记录重试后会被页面轮询立即判为超时。新增 `attempt_started_at` 后，创建和每次重开都会写入本次开始时间；原始 `created_at` 保持为生成记录的首次创建时间，收束仅依据本次开始时间。

- 2026-09-09：生成状态问题的真实任务已完成文字生成和自动审核，但旧前端将活动任务放在队列外且未区分审核拒绝与审核未启动。自动审核报告的规则层已通过，模型层指出术语通俗解释不足、存在需回查原文的绝对化表述。Phase 23 应将运行任务与草稿统一列在左侧队列，右侧读取阶段事件和审核结论；自动改稿最多两轮，保持来源事实字段不可改写。
- 2026-09-09：自动改稿版本需要独立审计快照，不能仅依赖 `content_drafts.version`，否则旧正文会被覆盖。新增 `draft_revisions` 只存改稿后的摘要、正文、标签及上轮可展示审核意见；来源字段不进入改稿输入的可修改字段，原文标题尾注由服务端重新附加。

- 2026-09-09：原自动配图在 `process_collection_job` 内串行调用图片服务，受 ARQ 全局默认 300 秒影响；Python 3.12 的 `asyncio.CancelledError` 不会进入 `except Exception`，造成父聊天任务没有终态回写。Phase 24 将图片拆为 `image_generation_jobs`，每张图持有独立公开任务 ID、ARQ ID、状态与错误摘要；采集任务只生成文字和入队。
- 2026-09-09：自动审核需要封面时也预先建立私有封面图片子任务；自动投递层不再同步调用图片模型。所有图片任务结束后才进入审核和可选公众号草稿创建，图片失败保留文字草稿并安全停止自动投递，不会发表。
- 2026-09-09：发布页的投递记录原本只在右侧平铺，无法按文章选择或按日期浏览。现按投递创建日期分组、显示状态点，并允许选择文章后查看当前投递详情；远端草稿箱/已发表列表仍只在用户点击同步时读取。
- 2026-09-09：聊天页“正在创建对话”仅在 `conversation === null` 时显示。当前本机 `/api/chat/sessions`、预检和选中会话详情均返回 HTTP 200（约 2–6 ms），日志也显示浏览器随后成功读取会话并发送消息；未发现服务端 4xx/5xx 或 CORS 拒绝。该画面无法在当前服务状态复现，属于初始化请求尚未完成/被中断时前端没有显式失败或重试状态的表现，而非数据库或 Agent 卡住。
- 2026-09-09：当前运行 `66f87883-2e9d-4cfc-9559-a9e7764e6ab6`（arXiv、自动配图和自动审核已请求）仍在 API 中显示 `running/文字生成中`，但 Worker 日志已确认 ARQ 的 300 秒 job timeout 在 01:55:30 取消了该协程。取消发生在 LangGraph 的持久化节点；Python 3.12 的 `CancelledError` 不会被现有 `except Exception` 失败回写分支捕获，因此 `chat_agent_runs` 未被更新为失败。随后线程内的数据库持久化在 01:55:41 才结束，说明存在超时后的残余写入；不得直接重试，以免产生重复或部分草稿。
- 2026-09-09：最新 arXiv 任务的候选采集成功，但 5 条候选在模型调用阶段均返回 `402 Insufficient Balance`，因此 `created=0`。此前汇总逻辑将“来源请求成功但单条生成全部失败”误判为 partial/completed，收束逻辑又取到首个文字事件，造成“正在生成”与“完成 0 条”并存。现已改为失败终态、最后文字事件回执；模型运营性错误使用可审计的来源受限确定性降级。
- 2026-09-09：公众号远端草稿箱同步的 HTTP 502 是应用安全泛化后的结果，不能仅凭前端文案认定为凭据或微信权限。MCP 适配层现记录安全类别与可公开返回码，不记录完整第三方文本、请求体或密钥。
- 2026-09-09：发布素材原先是全局列表，无法判定“属于哪篇文章”。新增 `draft_publication_assets` 多对多绑定；新上传素材必须携带当前草稿 ID，生成插图通过既有草稿插图关系自动出现。既有全局素材保持原样且不会被自动绑定。

- 项目初始仅包含 PyCharm/评测目录、空依赖的 `pyproject.toml`、`AGENT.md` 与 `process.md`。
- GitHub Trending 是公开 HTML 页面，没有稳定的官方 Trending REST API；采集器需要低频访问、解析失败标记和缓存边界。
- PostgreSQL 使用带 pgvector 扩展的 PostgreSQL 16 镜像，首版表结构暂不依赖向量列。
- 当前机器 Docker Client/Server 均为 29.7.2，`docker compose config --quiet` 校验通过。
- 项目 `.venv` 使用 uv 0.11.14 管理，Python 3.11.15，依赖应通过 `uv sync` 安装。
- Alembic 当前版本为 `0001_initial (head)`；FastAPI 健康检查确认 PostgreSQL 已连接。
- 固定演示资讯成功生成草稿；同一审核幂等键重复提交均返回同一 `ready_to_publish` 状态。
- GitHub Changelog 页面提供的官方 RSS 地址为 `https://github.blog/changelog/feed/`；网页工具因 RSS MIME 类型不提供预览，但链接来源已由官方页面确认。
- `collection_runs` 可独立记录每个来源的 `success`、`partial`、`failed`，真实采集证明 GitHub 失败时 arXiv、HN、RSS 仍能完成并生成草稿。
- 当前主机及应用容器把 `github.com` 解析为 `127.0.0.1`，因此 GitHub Trending 真实请求在连接层失败；这是当前环境结果，不代表解析器失效，也未修改系统 DNS/hosts/代理。
- arXiv、Hacker News 和 GitHub Changelog RSS 的限量真实采集成功，各生成 2 条待审核草稿。
- 发现现有 `.env` 配置了 OpenAI 兼容服务；首次验收意外触发外部模型后立即中断，并新增默认关闭的 `LLM_ENABLED` 开关。未读取、打印或修改密钥内容。
- PostgreSQL 事务内验证 GitHub 快照：daily 与 weekly 的排名和周期增星可独立保存，验证后回滚且未保留测试数据。
- 主 Agent 已实现为受控 LangGraph 编排器：只接受结构化 `collect` 指令和四个枚举来源，不使用 LLM 选择工具，也不提供任意 URL、文件、SQL、发布或 MCP 能力。
- `agent_runs` 是父级审计记录，`collection_runs.agent_run_id` 是子级追溯字段；真实 arXiv 验证中二者 ID 关联一致。
- 新增 `/api/agent/collect` 与 `/api/agent/runs`；原有 `/api/collections/{source}` 已改为兼容包装，真实验证仍成功。
- Phase 8 验收时容器迁移为 `0003_main_agent (head)`，并确认 `LLM_ENABLED=False`。
- 私有 MinIO 桶默认不设置匿名读取策略；附件下载仅经应用层 HMAC 短时链接。首版尚无用户登录，生产前需要补充身份与附件归属校验。
- Phase 9 容器迁移为 `0004_chat_attachments (head)`；本机前端 `127.0.0.1:5173`、MinIO API/控制台 `127.0.0.1:9000/9001` 和 API 均已响应。
- 附件闭环验收上传固定 Markdown 后，普通对话未读取附件；明确提取指令在 `LLM_ENABLED=False` 时返回拒绝说明，附件状态仍为 `uploaded`，短时下载成功。
- 审核接口在返回“附件来源”的草稿时会即时重签下载链接，避免把会过期的首次链接永久保存为审核来源。
- Phase 10 中的通用对话模型只能产出受限的意图决策；实际业务副作用仍由服务端按枚举意图选择固定 Tool，模型不能自行调用任意工具。
- 定时/发布计划的“确认”仅把计划和会话运行记录改为已确认，并追加审计事件；没有调度器、平台账号、平台 API 或真实发布副作用。
- 对话页的可展开区域是可审计执行摘要（意图、已调用 Tool、计划确认事件），不是原始 chain-of-thought，避免暴露不稳定推理和敏感运行细节。

外部资讯均作为不可信数据处理，不执行来源正文中的任何指令。

- 2026-09-09：附件原件当前已私有保存至 MinIO，PostgreSQL 仅保存对象键与元数据；项目已有 `SessionWorkspaceTool`，但上传后的图片尚未自动创建工作区副本。聊天选择控件可携带单个 `attachment_id`，因此第一版自然语言图片指令以“当前选中附件”为唯一显式引用对象，避免从会话历史中猜测用户意图。
- 2026-09-09：`chat_agent_events` 已按顺序持久化并随会话返回，可作为生成过程状态的唯一审计来源；无需为临时百分比额外保存不可靠的模型进度。前端将依据最新事件显示文字与图片分项状态，且不展示原始思维链。

- 2026-09-09：用户确认自动封面策略 A。自动审核通过时，系统仅可在缺少封面时经配置的图片生成供应商创建一张通用科技插画；之后最多创建微信公众号草稿，绝不调用发表提交。项目内图片生成 Skill 将采用 Agnes 的 OpenAI 风格图片生成请求契约作为可替换供应商实现参考，但不会读取、复制或复用 Hermes Skill 内任何密钥。

## 2026-09-09｜全局失败通知中心

- 原有 `chat_agent_runs`、`image_generation_jobs` 和 `wechat_publication_jobs` 已保存失败原因，但没有跨页面统一的未读状态，不能满足右上角提醒、已读和删除需求。
- 新增 `notifications` 仅保存通知视图状态与来源指针；删除操作采用隐藏标记，绝不删除底层任务、草稿、图片、审核或公众号投递审计。若同一来源出现新的错误内容，通知会自动恢复为未读。
- 首次读取 `/api/notifications` 会从已有失败审计补齐通知，不读取模型、图片或公众号服务。容器验证已得到 6 条历史失败通知，包含公众号投递与图片生成失败。

## 2026-09-09｜图片服务返回格式与队列界面

- 受用户明确授权的最小化 Agnes 图片请求返回 HTTP 200、1 条结果和 HTTPS URL，说明网络、凭据与模型服务可用；该请求未保存图片或修改草稿。
- 供应商同时给出空 `b64_json` 与有效 URL。旧适配器将空字符串解码为 0 字节并优先返回，导致后续校验/MinIO 保存失败；现已改为仅接受非空且可解码的 Base64，否则下载 HTTPS URL。
- 失败生成记录属于同一生成历史，不应与运行中任务混在“任务状态”区；页面改为运行中单列、失败项按日期与文案共同列出。

## 2026-09-09｜废弃草稿的来源去重边界

- 原 `content_drafts.source_item_id` 具备唯一约束，并且采集流程把来源已存在或内容哈希相同直接视作草稿重复；这会使已废弃草稿也无法重新生成。
- 去重现按相同内容哈希关联的草稿状态判定：`pending_review`、`needs_revision`、`ready_to_publish`、`published` 会阻止重复生成；`discarded`、`deleted` 不会阻止。来源、草稿、审核、发布记录均原样保留。
- 迁移 `0019_reusable_source_drafts` 已移除 PostgreSQL 的 `content_drafts_source_item_id_key`，实际数据库校验该约束计数为 0。

## 2026-09-09｜Worker 图片存储与自动审核收束

- 截图中的 3 个 Agnes 图片请求和 HTTPS 图片下载均返回成功；失败发生在 Worker 将图片保存到 MinIO 时，因 Worker 未注入 `MINIO_*` 环境变量而回退使用配置默认 `localhost:9000`。容器内该地址指向 Worker 本身而非 MinIO。
- Compose 已为 Worker 注入 `minio:9000` 及同一组私有存储凭据/桶配置；重建后从 Worker 只读检查确认目标桶可访问。
- 配图失败/超时不代表文字审核通过。收束任务现在保留文字草稿、写入“配图未完成”和“自动审核待人工处理”，并跳过自动审核批准及公众号草稿投递。

## 2026-09-09｜自动配图与插图定位 Skill

- 项目已有 `IllustrationPlanner`、独立图片任务和草稿插图绑定能力；新增 `app/skills/auto-illustration/SKILL.md` 将其固化为可审计顺序：先规划位置，再排队逐图生成，最后私有保存并按段落位置绑定。
- 图片不会写进正文纯文本；`DraftIllustrationRow.placement_after_paragraph` 保存位置，预览/发布适配器据此渲染。自动配图最多 3 张，无合适位置可返回空计划。
- 回归测试以确定性五段正文验证规划 `[2, 4]` 发生在图片调用之前，且每张图收到其对应段落上下文和独立插图 ID。

## 2026-09-09｜DeepAgent 会话记忆重构基线

- 已安装 `deepagents 0.7.13`、`langgraph-checkpoint-postgres 3.1.2` 和 `psycopg`；`create_deep_agent` 支持 `skills`、`memory`、`checkpointer` 与 `store` 参数，`AsyncPostgresSaver` 需要在首次使用时显式 `setup()`。
- 当前项目尚未调用 `create_deep_agent`。路由层先以空历史识别意图，再把预先生成的决策传给 `ConversationAgent`，因此数据库虽保存聊天消息，却不会稳定影响当前决策。
- 当前图片指令会从全局可编辑草稿列表取第一条；应改为会话记忆中的活动草稿，标题存在多个匹配时必须追问。
- DeepAgent 默认包含文件、Shell 和子代理能力；本项目只能注册受控业务 Tool，并禁止默认执行与任意文件能力。公众号 MCP 保持在明确确认的发布页面边界外。
- 2026-09-09：Phase 20 首次容器迁移失败并自动回滚：`alembic_version.version_num` 为 `varchar(32)`，新迁移 ID 超长。已将编号缩短到 32 字符以内；该修复只影响迁移元数据，未改动业务表或外部服务。

## 2026-09-09｜DeepAgent 持久记忆与能力边界

- 会话通用回复已接入 `create_deep_agent`；LangGraph PostgreSQL checkpoint 以 `chat_session.id` 作为 `thread_id` 保存上下文，首个真实会话调用时初始化其专用 checkpoint 表。
- `chat_session_memories` 保存可审计的业务指针：当前草稿、当前附件和简短摘要。路由和图片绑定仅使用该会话指针，不再从全局草稿列表猜测第一篇。
- DeepAgent 只拥有 3 个会话 Tool：读取当前上下文、列出可编辑草稿、在用户明确选择后设置当前草稿。默认子代理、Shell、写入/编辑文件、任意文件检索和发布能力均未提供。
- 项目 Skill 与运行代码边界记录在 `agent_skills/ARCHITECTURE.md`；Exa 与公众号 MCP 仍仅在受控采集/发布流程调用，不会作为聊天 Agent Tool。
- 配图任一失败或超时时，自动审核状态为“已中断”，跳过自动批准、公众号草稿创建及发表；图片提示词明确禁止任何文字、字母、数字、水印和 UI。

## 2026-09-09｜复合指令与 README 证据包诊断

- 当前 Docker Compose 未向 app/worker 注入 `DEEP_AGENT_ENABLED`；即使项目 `.env` 已设为 true，容器内实际仍为 false，通用对话会安全回退而不会运行 DeepAgent。
- 新会话中“生成封面图和插图”先被意图层识别为图片请求；该会话不存在活动草稿，路由会按安全规则拒绝而非跨会话猜测。带有明确采集动词和来源的复合命令应直接进入采集并开启自动配图。
- GitHub README 富化把 `/repos/{owner}/{repo}/readme` 的原始 JSON 直接写入 `RawSourceItem.content`，其中含 Base64 正文和 API URL，导致超长链接进入生成模型与 LangSmith。应仅解码 `encoding=base64` 的 `content` 字段。
- 生图供应商可能忽略负向提示词；要保证不出现中文或其他文字，必须在私有保存前增加 OCR 检测。检测失败应视为图片生成失败，保持草稿但不绑定图片；自动审核随之中断。

## 2026-09-09｜Phase 32 实施结果

- `decode_github_readme_response` 仅接受 GitHub README JSON 中 `encoding=base64` 的 `content` 字段，并在进入 `RawSourceItem.content` 前解码为 UTF-8 Markdown；API 元数据、`download_url` 与 Base64 原文不再进入模型。
- `rapidocr-onnxruntime` 延迟加载；生成图片在私有存储前检测可见文本。任一检测结果存在文字时最多重试两次，检测组件不可用或三次均失败则不保存图片，并由既有 Worker 边界中断自动审核。
- app 和 worker 的 Compose 环境都转发 `DEEP_AGENT_ENABLED`；采集命令中的自动配图字样会转换为后台 `auto_illustration` 任务参数。
- 项目虚拟环境中相关测试 18 项通过，空白 PNG 的 OCR 自检返回空列表；真实生图和公众号调用未执行。Docker 构建因 Debian 图形库下载较慢留在后台，未替换现有容器。

## 2026-09-10｜原记录重生成与自动改稿诊断

- 当前聊天路由一旦识别为 `collect_news`，必定调用 `create_chat_agent_run` 并入队普通采集任务；这正是“重新获取项目信息并生成文案”在生成列表中新增条目的原因。
- 草稿与生成运行记录当前只通过文字阶段事件中的 `draft_ids` 间接关联，缺少稳定外键；要可靠复用原记录，需要为草稿保存原 `chat_agent_run_id` 并回填历史事件关联。
- 自动改稿 Tool 将配置、鉴权、模型、网络和超时异常统一转换为“自动改稿模型暂时不可用”，调用方无法展示真实类别。自动审核也有同样的宽泛异常分支。
- 当前重生成应只覆盖待审核、退回修改或待发布草稿的可编辑字段；已发布与废弃草稿保持不可覆盖，以保护发布审计。
- 2026-09-10 运行日志显示最近三次 `auto_revision_failed` 的底层类型均为 `APITimeoutError`；运行容器确认 LLM 已启用、密钥和模型名均存在、`REQUEST_TIMEOUT_SECONDS=20.0`、`DEEP_AGENT_ENABLED=true`。因此当前“自动改稿模型暂时不可用”的直接原因是模型请求超时，而非 LLM 开关或密钥缺失。
- 已以 PostgreSQL JSONB 审计字段编译验证历史草稿到原 `ChatAgentRunRow` 的查询；无须添加新的草稿外键或迁移，已有生成记录即可复用。
- 重生成 Worker 会对 GitHub 草稿重新读取 README；其他已登记来源暂先复用已保存的来源证据再生成，避免泛化采集意外创建新选题。任何重生成失败都会保留原草稿版本。

- 2026-09-06：第三方 `wechat-official-account-mcp` 的 SSE 入口为 `GET /sse`，连接后返回带会话参数的 `POST /messages` 地址；草稿创建、发布提交与状态读取都通过 `wechat_draft`/`wechat_publish` 工具完成。创建草稿必须提供封面 `thumbMediaId`，正文插图的 URL 与封面媒体 ID 是两类不同返回值，因此本项目需要分开审计。MCP 服务仅部署在 Docker 内部网络；应用后端是唯一调用方。

- 2026-09-08：现有图片附件强制关联聊天会话，发布页只是全局读取这些附件，且没有单素材删除接口；独立发布素材表可避免删除聊天历史及其审计记录。公众号草稿 API 需要 HTML 作为传输字段，但审核文案可保持纯文本：只在适配器边界将每个非空行安全包装成段落，并将已选图片作为独立节点加入。

- 2026-09-08：发布素材上传到私有 MinIO 成功，但准备公众号素材时应用请求 `http://wechat-mcp:3000/sse` 获得 HTTP 502。第三方容器的 TCP 健康状态为 healthy 只证明端口打开，未验证 SSE 会话；当前失败发生在 MCP 初始化之前，尚未执行 `wechat_media_upload`。

- 2026-09-08：修复 app 对 Docker 内网服务的代理排除后，MCP SSE 返回 HTTP 200 但仍未发送会话事件。根因是第三方 `/sse` 路由与当前 MCP SDK 均调用 `writeHead(200)`；SDK 传输启动前的重复响应头阻塞了 endpoint 写入。项目自有 Docker 构建层现以精确匹配脚本删除第三方编译产物中的首个重复写入，上游结构变化时构建会失败而不会模糊替换。重建后 app 内部 socket 握手验证为 `sse_status=True`、`endpoint_event=true`；未调用公众号 Tool、上传、创建草稿或发布。

- 2026-09-08：第三方 SSE 路由原本只有 `GET /sse`，未将 SDK 生成的 session ID 映射到 `POST /messages`，因此 MCP `initialize` 固定返回 HTTP 404。项目自有构建补丁现维护 `sessionId → SSEServerTransport` 映射，并将 `POST /messages` 的 JSON 请求转交 `handlePostMessage`；会话关闭时清理映射。内部验证确认 `endpoint → initialize` 返回 HTTP 202，未调用任何 `tools/call`。

- 2026-09-05：后台 GitHub 聚合任务的运行事件序号与入队事件冲突：路由已写入序号 3 的“后台任务已创建”，Worker 又以序号 3 写完成或失败事件，触发唯一约束。结果是任务状态仍显示 `running`，但没有本次新增草稿或来源记录；修复前不能将其视为仍在生成。
- 事件写入已改为由仓储层锁定父级运行记录后计算下一个序号，避免 API 请求与 Worker 对同一运行记录使用固定序号。已使用原历史运行记录验证可在既有 1、2、3 之后写入序号 4；未重新执行采集。
- 2026-09-06：GitHub Trending 的“新增 0 条”最新任务并非无热点，而是 Worker 内请求报 `All connection attempts failed`。主 Agent 正确返回 `failed` 结果，但 Worker 曾无条件映射为聊天 `completed`；现已按 Agent 结果映射聊天失败/完成状态，并将来源错误展示在执行摘要中。
- 2026-09-06：将 Docker 出站代理改为 `host.docker.internal:7895` 后，Worker 可经代理建立连接，但 HTTPS 校验报 `CERTIFICATE_VERIFY_FAILED`。说明连接路径已恢复，而该本地代理的签发根证书未进入容器信任链；不能以关闭校验作为修复方案。
- 2026-09-06：有效的 SteamTools 根证书以项目私有只读挂载方式进入容器；入口脚本将其追加到 `certifi` 公共 CA 包并设置 `SSL_CERT_FILE`。使用项目实际 `httpx` 客户端访问 GitHub Trending 已返回 HTTP 200；根证书目录同时被 Git 与 Docker 构建上下文忽略。
- 2026-09-06：真实 GitHub 任务确认采集 19 项、聚合 5 项均正常；`ContentPipeline` 聚合分支调用 `self.generator`，但初始化未保存该依赖，导致聚合草稿回滚。现已保存生成器并用无数据库替身仓储回归测试覆盖该路径。
- 2026-09-06：项目介绍去重不能发生在 GitHub Trending 候选采集之前，因为此时没有仓库标识；现由主 Agent 在候选返回后、README 富化 Tool 调用前查询 PostgreSQL。`project_introductions` 有来源唯一约束，并在草稿成功创建的同一事务中写入；既有草稿也视为已介绍以兼容历史数据。
- 2026-09-06：HN 外链正文仅接受 HTTP(S) 和非本地/非 IP 私有地址，不做 DNS 重解析；单篇响应限制为 1 MB、正文保存上限为 500,000 字符、并发为 4。页面正文作为不可信文本，只用于来源证据和 LLM 受限摘录，不执行其中内容。
- 2026-09-06：审核通过（`ready_to_publish`）不等于实际发布。`publication_records` 是运营人员实际发布后回填的平台、链接和幂等键审计；只有该记录存在时 GitHub 草稿会转为 `published` 并创建/激活 `project_introductions`。已有的 `project_introductions.publication_id IS NULL` 记录不参与候选去重，避免旧实现误排除未发布项目。

# 2026-09-10｜分级模型路由设计

- 会话级 DeepAgent、正文生成/自动改稿、自动审核和插图位置规划原先均直接读取 `LLM_MODEL`；因此无法控制成本、时延和模型能力，也无法仅从日志辨别实际选择。
- 新增的四项任务模型配置必须为空时回退到 `LLM_MODEL`，而不是要求现有部署一次性补齐；这样未修改用户 `.env` 时，实际行为保持兼容。
- 增强正文的旧 `LLM_FAST_MODEL`/`LLM_REASONING_MODEL` 仍仅是正文内部阶段的兼容配置；新的 `CONTENT_LLM_MODEL` 是正文与自动改稿的统一入口，审核和配图规划不再隐式继承它。
- 静态编译、差异格式检查、Compose 配置校验和 app/worker 容器内的回退/独立覆盖断言均通过；服务启动日志无异常、`/docs` 返回 HTTP 200，期间没有发起真实模型调用。

# 2026-09-10｜分级模型服务地址

- 模型名称和服务地址需要独立选择：仅拆分模型名仍无法让不同任务切换到不同 OpenAI-compatible 网关。
- 四项 `*_OPENAI_BASE_URL` 与模型配置一一对应；为空时回退 `OPENAI_BASE_URL`。当前阶段共用 `OPENAI_API_KEY`，不复制或记录用户凭据。
- app/worker 容器内的模拟回退与独立覆盖断言通过，本地服务正常启动；未向任一地址发起模型请求。

# 2026-09-10｜分级模型凭据

- 每类模型若来自不同网关，仅有独立模型名和 Base URL 不足；必须同时支持该任务专用 API Key。
- 任务 API Key 优先于统一 `OPENAI_API_KEY`，留空时自动回退，确保旧配置兼容。密钥不会记录在日志、事件、模型提示词或过程文档中。
- `.env` 已按“会话、正文/改稿、审核、插图规划”将模型、地址和密钥放在同一块，容器仅通过 Compose 显式转发这些字段。

# 2026-09-10｜Pydantic 会话决策兼容

- 当前 `ConversationDecision` 已是 Pydantic 模型，DeepAgent 也已使用该模型作为 `response_format`；截图中的失败是网关没有返回框架预期的 `structured_response`，不是 Pydantic 缺失。
- 兼容层只在原生结构字段缺失时读取最后一条助手消息，接受完整 JSON 或单个 JSON 代码围栏，再调用 `ConversationDecision.model_validate`。用户消息、自由文本、JSON 数组和 Schema 不匹配的对象均被拒绝。
- 日志只保存 `native`/`final_json` 来源或失败原因代号，不保存模型原文；业务白名单仍只在校验成功后执行。

# 2026-09-10｜工具式 Pydantic 会话决策修复

- 失败请求并未进入自定义 Pydantic 兼容解析：`response_format=ConversationDecision` 被 LangChain 识别为 ProviderStrategy，网关返回普通文本时会在框架内部解析 JSON 并抛出 `ValueError`。
- 更直接的运行时阻断来自 `FilesystemMiddleware(backend=..., tools=[])`：容器的 Deep Agents 版本要求该中间件至少含 `read_file`，因此旧代码会在创建 Agent 前抛出 `ValueError`，模型、工具和决策均不会执行。
- 现显式使用 `ToolStrategy(ConversationDecision)`，并移除不兼容的空文件中间件；安全 Harness Profile 仍会排除默认文件、Shell 与子代理能力。失败日志现标注 `agent_creation`、`agent_invoke` 或 `decision_validation`，不记录模型原文、用户正文或密钥。
- 容器内替身装配与真实 DeepAgent 图创建探测均通过，探测使用不可达本地地址且没有调用模型；app `/docs` 返回 200，app/worker 均处于运行状态。镜像不含 pytest，故未运行 pytest 命令。
# 2026-09-10｜长文模型超时诊断

- Worker 日志显示 GitHub Trending 和 README 抓取在约 5 秒内完成；随后正文模型调用从 16:50:40 开始，在 20 秒时限下经过 SDK 两次自动重试，于约 62 秒后以 `APITimeoutError` 降级。
- 运行中 Worker 的最小模型探测在约 1.35 秒成功返回，说明当前端点、凭据和模型名可用。
- 同一 Worker 的 JSON 协议探测在约 1.09 秒返回 `finish_reason=length`、最终 content 为空、推理内容存在；该模型会先消耗推理输出预算，不能将短响应时间等同于长结构化正文可在 20 秒内完成。
- 修复策略：保留聊天与来源 HTTP 的 20 秒通用时限；正文、自动改稿、审核和配图规划使用 90 秒独立时限，且单次请求不由 SDK 重复等待。
# 2026-09-10｜会话上下文缺失诊断

- 截图对应会话已保存“采集完成：新增 0 条待审核草稿”和后续追问，但 `chat_session_memories` 的活动草稿与摘要均为空。
- 采集命令由后台 Worker 执行，不会进入 DeepAgent checkpoint；旧逻辑只在创建草稿时写入会话摘要，因此零新增结果没有桥接到 DeepAgent。
- DeepAgent checkpoint 只保存自身参与的图状态，不能自动读取 API 聊天表和后台任务事件；意图识别的 `ValidationError` 会回退为通用对话，但不是本次丢失任务结果的主因。

# 2026-09-10｜聊天指令延迟回显与无响应诊断

- 前端此前只在 `POST /chat/sessions/{id}/messages` 返回后重新加载消息；后端又在同一 HTTP 事务内同步调用 DeepAgent，因此用户消息和数据库记录都会等待模型完成。
- 运行日志显示普通对话的 DeepAgent 请求曾持续数十秒至数百秒；卡住会话在“会话运行已开始”后没有完成记录，数据库也没有已提交的用户消息或运行记录。
- 现改为仅用本地规则识别明确采集/配图等受控指令；一般对话先提交用户消息和运行审计，再由 ARQ Worker 执行 DeepAgent。DeepAgent 总时限为 45 秒，单次 SDK 请求无额外重试。
- 关键日志使用会话 ID、运行 ID、消息 ID、内容长度、附件标记和错误类型，不记录用户正文、密钥或模型完整输出。

# 2026-09-10｜自动改稿失败与正文结构诊断

- 最近失败的自动审核记录显示：旧版本正文仅 284 字符，先触发旧的 900–1400 字符规则；随后自动改稿调用发生 `APITimeoutError`。当时模型时限为 20 秒且 SDK 重试两次，实际等待约 62 秒后失败。
- 该历史记录展示为“自动改稿模型暂时不可用”；当前长文配置已改为 120 秒、无 SDK 重试，新的失败会按错误类型显示安全提示。
- 正文生成与自动改稿现在要求内部 `body_sections` 恰好 4 项；每项可含 1–2 个自然段，服务端在合并为无标题纯文本前检查最低字数并记录每部分字符数。审核总长度改为 800–3200 字符、自然段为 4–8 段。

# 2026-09-10｜会话级 DeepAgent 统一决策诊断

- 现有路由先调用 `OpenAICompatibleConversationModel.decide()`，普通对话再进入 ARQ 后台 `ContentDeepAgent.respond()`；同一条消息存在独立意图模型与 DeepAgent 两段模型链路。
- 截图对应请求的意图模型 HTTP 请求返回 200，但结构化结果触发 `ValidationError` 后安全降级；随后 DeepAgent 在 18.7 秒内多轮请求，最终收到供应商 HTTP 429。现有 Worker 将所有空回复错误标为超时，造成提示失真。
- 已有 `ContentDeepAgent` 以 `chat_session.id` 作为 PostgreSQL checkpoint 的 `thread_id`，并有会话记忆/受控上下文 Tool 基础；本阶段应复用该状态，不新建记忆表。
- 本次 `uv run` 尝试仅用于读取已安装 Deep Agents 签名，但受限环境无法查询解释器（os error 5），未执行代码、未修改依赖或配置；后续改用项目虚拟环境的解释器进行只读检查。
- 容器镜像的 Dockerfile 原先未复制 `agent_skills/`，而 `ContentDeepAgent._agent_files()` 会在实际模型调用前读取该目录。静态导入无法发现这一缺口；需将该项目内运行依赖复制入 app/worker 镜像后再验证。

# 2026-09-10｜README 上下文保留方案

- GitHub README 成功抓取后，之前只在 `source_items.content` 中保留当前来源内容；重生成仍会先走 GitHub 请求，因此限流或网络失败会让模型只看到项目元数据并产生泛化文案。
- 现按草稿建立一对一私有对象快照：仅记录来源 URL、内容来源、哈希、长度、捕获/删除时间；正文只保存在 MinIO 私有对象中，不进入草稿 API、聊天上下文或日志。
- 重生成优先读取未删除快照；审核通过时删除 MinIO 对象并将 `object_key` 置空。没有快照的旧草稿保留既有重新抓取行为。

# 2026-09-10｜GitHub 候选顺序与页面职责核对

- 自动流程的真实时序为：文字/图片生成 → 自动审核 → 草稿审核通过 → 上传图片素材并创建公众号草稿；因此自动审核是投递的前置条件，而不是公众号发布后的步骤。
- 当前已发布项目排除在排序前执行；但有效草稿内容去重发生在选出最高候选后，最高候选重复会使该轮不创建草稿，低排名的新项目不会递补。
- 当前数据库 `project_introductions` 中带 `publication_id` 的排除记录为 0 条；没有历史排除名单可删除。

# 2026-09-10｜遗留生成记录与聊天交互诊断

- `chat_agent_runs` 仅按 `status=running` 查询，现有列表接口不会依据创建时间自动结束遗留任务；Worker 只有在正常完成、异常或取消路径才会结束父运行，因此 Worker/容器中断会留下“正在生成”记录。
- 聊天页面没有消息容器引用或切换会话后的滚动副作用；因此加载历史会话后维持浏览器默认滚动位置，而非最新消息。
- 自动配图与自动审核当前均初始化为 `false`，现有标签样式没有区分启用状态。
- 已实现页面读取时的安全收束：阈值取 `max(collection_job_timeout_seconds, image_generation_job_timeout_seconds) + 300`，默认 1500 秒。被收束的记录转为失败、更新占位回复、写入审计事件与通知，但不删除草稿或图片。

# 2026-09-10｜遗留运行收束与队列删除

- 指定运行 `d284a710-ba08-454b-ab0f-0b6e587b1f7b` 因超过 25 分钟安全等待窗口已被收束为 `failed`；错误说明和最后审计事件均为“已有草稿和图片均已保留”。
- 关联草稿 `0905c195-2d70-4b87-b256-ccf859a56638` 仍为 `pending_review`，当前关联插图数为 1。
- 生成队列删除仅允许结束态 `collect_news` 运行；会删除该运行审计、运行事件、图片任务审计和关联通知，保留草稿、插图文件、聊天会话/消息、审核和发布数据。

# 2026-09-10｜内容模型超时与重试

- 提供的调用堆栈确认内容模型请求在代理链路已经建立后、等待首个响应头阶段触发 `ReadTimeout`，SDK 最终抛出 `APITimeoutError`；不是 README 获取、密钥缺失或结构化输出校验失败。
- 生成器不再在可恢复的供应商错误后创建确定性草稿；会抛出经过脱敏的 `GenerationError`，由生成记录显示为失败并保留既有草稿。
- 当前运行时仍从用户 `.env` 获得 `CONTENT_LLM_TIMEOUT_SECONDS=120` 和 `COLLECTION_JOB_TIMEOUT_SECONDS=600`；项目默认及 `.env.example` 已更新为 600/900，但不会覆盖真实 `.env`。

# 2026-09-11｜发布页对象渲染与来源写作改进诊断

- 发布页崩溃直接发生在 `frontend/src/App.tsx` 的自动审核意见列表：后端历史 `model_report.issues` 允许模型原样保存数组项，而部分模型返回 `{type, description}` 对象；React 不能把对象直接作为子节点。生成记录页有同一段渲染风险。
- 修复需要两层：后端将新写入的审核意见规范为短文本，前端将历史未知项安全转为文本，避免旧审计数据再次导致页面整体失败。
- 当前项目的 `agent_skills/` 会由 `ContentDeepAgent._agent_files()` 读取并随会话挂载；正文生成实际由 `app/services/generator.py` 运行，尚未按来源加载写作 Skill。四类来源 Skill 必须同时被生成器读取，才会真正影响文案生成。
- 当前增强生成已有受控 Exa MCP 搜索，但仅允许模型为术语解释决定检索。后续可扩展为背景、技术优点与部署语境的证据补充，仍限制最多两条查询、只允许登记的 Exa Tool、所有新增事实必须在返回证据中引用。

# 2026-09-11｜自动化投递状态与待确认架构调整

- 当前没有运行中的 `chat_agent_runs` 或图片后台任务。最新采集运行 `c2c2208b-a834-4422-adb6-6be506999a34` 已完成文字、配图和自动审核；草稿 `d19efcf5-fc15-43f8-9ba1-a88de5f880d1` 为 `ready_to_publish`。
- 该草稿的自动投递在创建公众号草稿阶段因 MCP SSE HTTP 502 结束为 `delivery_failed`；远端未返回草稿 media id，因此不能视为完成，也尚未写入去重记录。
- 现有自动配图会由 `IllustrationPlanner` 规划正文插图位置，但封面固定在勾选自动审核时创建；发布页面另有人工选择封面和插图的单独流程。自动投递则直接选择已绑定封面及最多 6 张正文图。
- 自动审核目前先执行硬规则，再让模型仅返回 `passed/issues/summary`；不含分数或严重度。失败后最多进行两次“改稿→复审”，所以后一次评审可能继续提出新问题。
- 当前 `mark_wechat_draft_created()` 只保存公众号草稿创建状态；`project_introductions` 仅在真实发布回调时为 GitHub 来源写入，故个人号流程需要新增明确的草稿箱终态和去重写入点。

# 2026-09-11｜Phase 49 实施决策

- 已按用户确认采用四类来源去重和 85 分通过阈值。保留现有 `project_introductions` 表作为来源去重审计，草稿箱成功记录不伪造 `publication_records` 或真实发表链接。
- 公众号草稿成功将草稿状态改为 `draftbox_created`；仅在 MCP 返回远端草稿 media id 后才写入去重。素材上传、模型选择或草稿创建失败均不写入去重。
- 自动审核改为要求模型一次返回评分、完整问题清单和严重度。固定来源/格式规则仍是硬门槛；模型分数达到阈值但有 critical 问题时不会通过。自动改稿最多一次，随后只做一次最终复核。
- 发布页将改为草稿箱投递视图并禁用提交发表 API；生成记录不再展示人工勾选封面和正文插图，改为由模型从已生成图片中选择。

# 2026-09-11｜Phase 49 最终验证结论

- 发布素材准备请求已无人工封面或插图参数；模型只可从当前草稿已有的生成图片中选择，异常会返回明确错误且不进入去重。
- 审核输出现在持久化评分、阈值、严重问题计数与规范化意见。评分达到 85 分且没有 critical 问题时通过；完整意见应在一次评审中返回，自动改稿上限为一轮。
- `draftbox_created` 是个人号流程的最终成功状态。只有远端微信草稿标识已返回，才会为 arXiv、GitHub、Hacker News、RSS 统一写入来源去重审计；未创建草稿箱的失败任务不会被去重。
- 无需数据库迁移：草稿状态为既有字符串字段，来源去重审计表已存在。应用镜像替身检查、前端类型检查与生产构建、OpenAPI 合约、410 防发表端点及 HTTP 健康检查均通过。

# 2026-09-11｜公众号 MCP 白名单失败与投递预览

- MCP 容器日志已确认微信官方接口返回 `40164 invalid ip`。请求已到达微信，认证流程在出口 IP 白名单校验处停止；后续 `ECONNRESET` 是失败后的连接收束，并非根因。白名单修复前不能据此判断 AppSecret 或草稿接口权限。
- `_third_party_tool_error()` 现在优先识别 `40164`/`invalid ip`，返回“在公众号后台 IP 白名单添加当前出口 IP 后重新创建草稿箱”的安全操作提示；不将 IP 地址、凭据或第三方原文写回界面。
- 草稿箱投递页面已拆分审核提示与投递提示。自动审核结果仍留在审核区；创建草稿箱或远端草稿箱同步错误显示在“当前草稿箱投递”卡片。
- 草稿预览从纯文本升级为当前文章的封面、段落和模型选中正文插图的完整布局，插图按 `placement_after_paragraph` 插入；使用已有私有素材下载地址，不触发公众号请求。
- `wechat-mcp` 容器运行时已使用 `WECHAT_MCP_*_PROXY` 独立变量，默认留空即直连微信；镜像构建阶段仍可使用 `OUTBOUND_*` 下载依赖。实际容器三种代理变量均为空。
# 2026-09-11｜Phase 54 受限文案与审核子 Agent

- 用户已确认：将文案生成与自动审核的内容判断整合为受限子 Agent；配图规划继续保持轻量结构化调用。
- 安全边界：子 Agent 只接收已整理的证据/草稿/规则结果，并只返回经 Pydantic 校验的结构化内容；不拥有数据库写入、删除、图片生成、微信草稿箱投递或去重权限。
- 待实施：复用项目现有 `create_deep_agent` 与模型任务级配置，不读取真实 `.env`、不调用外部服务。

## Phase 54 实施发现

- 文案生成原有基线调用与增强模式的最终写作调用均为直接 OpenAI SDK 请求；增强模式的检索规划、选题规划和质检仍是轻量 JSON 调用，受控 Exa 搜索边界不改变。
- 自动审核原有模型调用也为直接 SDK 请求；固定格式规则、分数阈值、改稿轮次、草稿箱投递和数据库状态均在 Agent 之外。
- 新增的 `RestrictedContentTaskAgent` 每次调用临时创建 `create_deep_agent`，显式传入空 Tool、空 Skill、空 Memory、空 Subagent 和内存后端；不会创建 PostgreSQL checkpoint，也不能产生副作用。
- 增强生成保留 `LLM_FAST_MODEL` 的检索/规划/质检使用方式；最终长文写作改走 `LLM_REASONING_MODEL`（若留空则回退内容模型）的文案子 Agent，仍复用内容模型的地址和密钥配置。
- 审核子 Agent 输出 `score`、`issues` 和 `summary` 的 Pydantic 模型；本地审核 Tool 再执行严重度计数、85 分阈值及展示文本规范化，模型不能自行通过审核或改写数据库。

# 2026-09-11｜本地微信公众号 Skill/Tool 迁移发现

- 本地可迁移 Skill 已存在于 `agent_skills/wechat-official-account/`：其脚本直接使用微信官方 REST API、显式 `trust_env=False`，并已保存当前账号的只读权限审计与官方文档链接。
- 运行中的应用仍由 `app/services/wechat_mcp.py` 经 SSE 调用 `wechat-mcp` 容器；自动投递、草稿箱读取和旧的已发表列表均未复用本地 Skill。
- MCP 的 1 MB 正文图片拦截发生在其本地 Node 实现，不能作为微信官方接口限制结论。迁移后取消应用本地预检，让官方接口返回真实限制；失败信息仍脱敏显示。
- 本阶段只迁移到官方草稿箱和只读草稿列表。个人号流程不需要且当前未获授权的已发表文章读取保持禁用，不再为该路径保留 MCP 回退。
- 已删除应用 MCP 适配器、专项 MCP 测试和 Compose 的 MCP 服务/依赖；当前运行服务列表只包含 app、worker、frontend、PostgreSQL、Redis 与 MinIO。
- 本地 Tool 将官方 `40164`、`48001`、凭据和网络类错误映射为脱敏操作提示；日志只记录操作名、异常类型和受控错误码。素材上传和草稿创建仍需真实业务流程才会访问微信。
- 应用镜像已验证实际装载 Skill 脚本的客户端，且 `trust_env=False`；替身投递验证确认 2 MB 正文图不会在应用层被 1 MB 规则拒绝，最终大小/格式限制由官方接口决定。

# 2026-09-12｜结构化输出模式与一致性核对

- 文案生成子 Agent 本身没有业务工具，但为取得 Pydantic 结构化结果使用了 `ToolStrategy(DraftWritingResponse)`；该策略会向模型发送 `tool_choice: required`，思考模式模型网关直接以 400 拒绝，请求在生成前失败。这不是提示词、证据包或素材问题。
- 修复采用“两种模式 + 配置选择”：`tool` 保持强制工具调用（普通模型与已稳定运行的会话模型），`json` 只要求 JSON 文本并由本地 Pydantic 校验。关键点是两套指令必须互斥——若在 `json` 模式下仍保留“必须调用结构化工具”的句子，模型仍可能触发同样的 400，因此改为按模式注入输出指令，并把两个子 Agent 的基础系统提示词改为不含工具要求。
- `json` 模式只在最终助手消息中解析 JSON（允许单个 ```json 代码围栏），并区分“没有结构化结果”“不是可解析 JSON”“不是对象”“不符合 Schema”四类失败；全部走明确报错，不生成兜底文案、不执行业务操作，符合“模型输出不合规时明确报错”的既有边界。
- 会话 DeepAgent 原本已有“原生结构缺失则解析最终 JSON”的兼容层，因此 `json` 模式只需省略 `response_format`，复用既有 `_decision_from_agent_result()`，无需新增解析路径。
- 配置回退顺序（任务级 → `LLM_STRUCTURED_OUTPUT_MODE` → `tool`）保证了未修改配置的部署行为不变；同时按任务覆盖是必要的，因为当前三类任务使用不同网关与模型，思考模式与否并不一致。
- 11 项失败测试全部是“测试未跟随实现变更”，逐项根因：GitHub 取消原文标题尾注（Phase 53）、正文最低 1200 字（Phase 51）、供应商故障不再生成确定性兜底（Phase 48，`findings.md` 已记录该决定但用例未更新）、仓储新增 `get_wechat_publication_for_draft()`（替身缺 `scalar()`）、Worker 改用 `ContentMainAgent(..., settings=settings)` 构造且失败事件标题/额度文案变化。
- 前端死代码清单经引用计数确认：`ActiveGenerationPanel`、`ReviewPage`、`LegacyWechatDraftPreparation`、`LegacyPublishingPage`、`LegacyPublishingPage2` 与仅供它们使用的 `previewTextLines` 均无 JSX 引用；而 `AgentWechatDraftPreparation`（经 `WechatDraftPreparation` 别名）与 `GeneratedIllustrationList` 仍被生成记录页使用，必须保留——删除前先做引用计数可避免误删。
- 本地测试环境事实：app/worker 容器运行时占用 `runtime_logs/news-agent.log`，`import app.worker` 会在导入期写日志，因此直接运行 pytest 会在收集阶段报 `PermissionError`；把 `LOG_FILE` 指向临时路径即可。pytest 的 `tmp_path` 基目录位于受限沙箱临时区，无法创建，2 项用例改在项目内临时目录执行等价脚本验证通过。
