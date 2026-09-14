from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """所有运行参数均来自环境变量，不在代码中保存密钥。"""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "资讯运营 Agent"
    app_env: str = "development"
    log_level: str = "INFO"
    log_file: str = "runtime_logs/news-agent.log"
    database_url: str = (
        "postgresql+psycopg://news_agent:change-me@localhost:5432/news_agent"
    )
    openai_base_url: str | None = None
    # 地址可按任务覆盖；留空时回退到统一 OPENAI_BASE_URL。
    conversation_openai_base_url: str | None = None
    content_openai_base_url: str | None = None
    review_openai_base_url: str | None = None
    illustration_planner_openai_base_url: str | None = None
    evidence_selector_openai_base_url: str | None = None
    openai_api_key: str | None = None
    conversation_openai_api_key: str | None = None
    content_openai_api_key: str | None = None
    review_openai_api_key: str | None = None
    illustration_planner_openai_api_key: str | None = None
    evidence_selector_openai_api_key: str | None = None
    # 设置页新增的模型档案密钥使用 Fernet 加密后才会落库。此密钥仅来自私有 .env，绝不通过 API 返回。
    model_profile_encryption_key: str | None = None
    llm_model: str | None = None
    # 按任务拆分模型：留空时回退到既有 LLM_MODEL，升级配置不会中断现有调用。
    conversation_llm_model: str | None = None
    content_llm_model: str | None = None
    review_llm_model: str | None = None
    illustration_planner_llm_model: str | None = None
    evidence_selector_llm_model: str | None = None
    llm_enabled: bool = False
    enhanced_generation_enabled: bool = False
    llm_fast_model: str | None = None
    llm_reasoning_model: str | None = None
    # 结构化输出模式：tool 强制调用结构化工具（普通模型）；json 只要求 JSON 文本并由本地 Pydantic 校验。
    # 思考模式模型会拒绝 tool_choice=required，因此按任务选择；留空时回退统一开关。
    llm_structured_output_mode: str = "tool"
    conversation_structured_output_mode: str | None = None
    content_structured_output_mode: str | None = None
    review_structured_output_mode: str | None = None
    exa_mcp_enabled: bool = False
    exa_mcp_url: str = "https://mcp.exa.ai/mcp?tools=web_search_exa,web_fetch_exa"
    exa_api_key: str | None = None
    exa_search_max_queries: int = 2
    exa_search_max_results: int = 5
    exa_search_result_max_chars: int = 6000
    # 把联网补充搭在“本来就会发生”的那次改稿上：不增加模型调用，只增加 1–2 次检索。
    revision_search_enabled: bool = True
    # 公众号草稿的留言设置：微信在这两个字段缺省时按 0（关闭留言）处理，必须显式给出。
    wechat_open_comment: bool = True
    wechat_only_fans_can_comment: bool = False
    # 投递选图是否把真实截图交给视觉模型看：先识别官方图内容，再决定封面与正文插图。
    publication_vision_selection_enabled: bool = True
    deep_agent_enabled: bool = False
    agent_script_timeout_seconds: int = 15
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_secure: bool = False
    minio_bucket: str = "chat-attachments"
    agent_workspace_dir: str = "/app/agent_workspace"
    attachment_download_secret: str = "change-me-before-production"
    attachment_link_ttl_seconds: int = 900
    attachment_max_bytes: int = 2 * 1024 * 1024
    app_base_url: str = "http://127.0.0.1:8000"
    arxiv_categories: str = "cs.AI,cs.CL,cs.LG"
    rss_feeds: str = "https://github.blog/changelog/feed/"
    collect_limit: int = 25
    # 可选：GitHub API 令牌。未配置时 README 接口只有每小时 60 次的匿名配额，容易被限流。
    github_token: str | None = None
    # 生成插图单独的大小上限：聊天附件的 2 MB 限制不适用于模型生成的图片。
    generated_image_max_bytes: int = 10 * 1024 * 1024
    # 采集和普通对话保持较短超时；正文、改稿与审核为结构化长输出，使用独立时限。
    request_timeout_seconds: float = 20.0
    conversation_agent_timeout_seconds: float = 45.0
    content_llm_timeout_seconds: float = 600.0
    content_llm_max_retries: int = 0
    langsmith_tracing: bool = False
    langsmith_api_key: str | None = None
    langsmith_project: str = "cs-news-agent"
    langsmith_endpoint: str = "https://api.smith.langchain.com"
    langsmith_workspace_id: str | None = None
    source_response_max_bytes: int = 1_000_000
    source_content_max_chars: int = 500_000
    llm_evidence_max_chars: int = 12_000
    # 目标字数（不是硬区间）：硬区间由 article_length_band() 在目标上下各放宽 200 字推导。
    draft_body_min_chars: int = 1600
    draft_body_max_chars: int = 2200
    # 自动审核必须同时满足硬规则与此评分阈值；0-100 分由审核模型一次给出。
    auto_review_pass_score: int = Field(default=85, ge=0, le=100)
    redis_url: str = "redis://localhost:6379/0"
    source_stale_after_minutes: int = 120
    # 微信本地 Skill 直接请求官方 REST API，显式不继承通用 OUTBOUND_* 代理。
    wechat_app_id: str | None = None
    wechat_app_secret: str | None = None
    wechat_api_timeout_seconds: float = 45.0
    # 图片服务与自动投递均为显式开关；缺少任一配置时自动化安全停止。
    image_generation_enabled: bool = False
    image_generation_provider: str = "agnes"
    image_generation_base_url: str = "https://apihub.agnes-ai.com"
    image_generation_api_key: str | None = None
    image_generation_model: str = "agnes-image-2.5-flash"
    # Agnes 2.5 推荐以尺寸档位搭配画幅比；4:3 更适合公众号图文阅读版式。
    image_generation_size: str = "1K"
    image_generation_ratio: str = "4:3"
    image_generation_timeout_seconds: float = 900.0
    collection_job_timeout_seconds: int = 900
    image_generation_job_timeout_seconds: int = 1200
    auto_wechat_draft_enabled: bool = False
    cors_origins: str = (
        "http://localhost:3000,http://127.0.0.1:3000,"
        "http://localhost:5173,http://127.0.0.1:5173"
    )

    @property
    def arxiv_category_list(self) -> list[str]:
        return [value.strip() for value in self.arxiv_categories.split(",") if value.strip()]

    @property
    def rss_feed_list(self) -> list[str]:
        return [value.strip() for value in self.rss_feeds.split(",") if value.strip()]

    @property
    def cors_origin_list(self) -> list[str]:
        return [value.strip() for value in self.cors_origins.split(",") if value.strip()]

    @property
    def generation_run_stale_timeout_seconds(self) -> int:
        """仅在最长文字/图片任务都应结束后的缓冲期才收束遗留运行记录。"""
        return max(
            self.collection_job_timeout_seconds,
            self.image_generation_job_timeout_seconds,
        ) + 300

    def model_for(
        self,
        task: Literal["conversation", "content", "review", "illustration_planner", "evidence_selector"],
    ) -> str | None:
        """按任务选择模型；新配置为空时保持 LLM_MODEL 的历史行为。"""
        configured = {
            "conversation": self.conversation_llm_model,
            "content": self.content_llm_model,
            "review": self.review_llm_model,
            "illustration_planner": self.illustration_planner_llm_model,
            "evidence_selector": self.evidence_selector_llm_model,
        }[task]
        return configured or self.llm_model

    def base_url_for(
        self,
        task: Literal["conversation", "content", "review", "illustration_planner", "evidence_selector"],
    ) -> str | None:
        configured = {
            "conversation": self.conversation_openai_base_url,
            "content": self.content_openai_base_url,
            "review": self.review_openai_base_url,
            "illustration_planner": self.illustration_planner_openai_base_url,
            "evidence_selector": self.evidence_selector_openai_base_url,
        }[task]
        return configured or self.openai_base_url

    def api_key_for(
        self,
        task: Literal["conversation", "content", "review", "illustration_planner", "evidence_selector"],
    ) -> str | None:
        configured = {
            "conversation": self.conversation_openai_api_key,
            "content": self.content_openai_api_key,
            "review": self.review_openai_api_key,
            "illustration_planner": self.illustration_planner_openai_api_key,
            "evidence_selector": self.evidence_selector_openai_api_key,
        }[task]
        return configured or self.openai_api_key


def structured_output_mode_for(
    settings: Settings,
    task: Literal["conversation", "content", "review"],
) -> str:
    """按任务选择结构化输出模式；新配置为空时保持强制工具调用的历史行为。"""
    configured = getattr(settings, f"{task}_structured_output_mode", None)
    mode = str(configured or getattr(settings, "llm_structured_output_mode", None) or "tool").strip().casefold()
    if mode not in {"tool", "json"}:
        raise ValueError(f"不支持的结构化输出模式：{mode}")
    return mode


def model_for(
    settings: Settings,
    task: Literal["conversation", "content", "review", "illustration_planner", "evidence_selector"],
) -> str | None:
    """兼容测试替身与旧调用方的任务模型选择入口。"""
    configured = _stored_model_profile_value(settings, task, "model_name")
    return configured or getattr(settings, f"{task}_llm_model", None) or getattr(settings, "llm_model", None)


def base_url_for(
    settings: Settings,
    task: Literal["conversation", "content", "review", "illustration_planner", "evidence_selector"],
) -> str | None:
    """兼容测试替身与旧调用方的任务服务地址选择入口。"""
    configured = _stored_model_profile_value(settings, task, "base_url")
    return configured or getattr(settings, f"{task}_openai_base_url", None) or getattr(settings, "openai_base_url", None)


def api_key_for(
    settings: Settings,
    task: Literal["conversation", "content", "review", "illustration_planner", "evidence_selector"],
) -> str | None:
    """任务密钥优先；留空时兼容既有 OPENAI_API_KEY。"""
    configured = _stored_model_profile_value(settings, task, "api_key")
    return configured or getattr(settings, f"{task}_openai_api_key", None) or getattr(settings, "openai_api_key", None)


def _stored_model_profile_value(
    settings: Settings,
    task: Literal["conversation", "content", "review", "illustration_planner", "evidence_selector"],
    field: Literal["model_name", "base_url", "api_key"],
) -> str | None:
    """读取设置页的模型绑定；数据库不可用时必须无感回退到 .env。"""
    try:
        from app.services.runtime_settings import model_profile_value

        return model_profile_value(settings, task, field)
    except Exception:
        return None


@lru_cache
def get_settings() -> Settings:
    return Settings()
