"""自动改稿与自动审核的提示词边界、以及改稿结构失败的可诊断性。"""

from __future__ import annotations

import inspect
import json
from types import SimpleNamespace

import pytest

import app.tools.auto_review as auto_review_module
import app.tools.auto_revision as auto_revision_module
from app.agents.content_task_agents import ReviewIssue, ReviewResponse
from app.domain.models import ContentCategory, NormalizedItem, SourceKind
from app.services.generator import (
    DeterministicDraftGenerator,
    OpenAICompatibleDraftGenerator,
    _natural_article_instruction,
)
from app.tools.auto_review import AutoReviewTool
from app.tools.auto_revision import AutoRevisionError, AutoRevisionTool, RevisionPayload


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        llm_enabled=True,
        llm_model=None,
        openai_api_key=None,
        openai_base_url=None,
        content_llm_model="content-model",
        content_openai_api_key="content-key",
        content_openai_base_url="https://content.example/v1",
        review_llm_model="review-model",
        review_openai_api_key="review-key",
        review_openai_base_url="https://review.example/v1",
        content_llm_timeout_seconds=600,
        content_llm_max_retries=0,
        draft_body_min_chars=800,
        draft_body_max_chars=4000,
        auto_review_pass_score=85,
        langsmith_tracing=False,
        langsmith_api_key=None,
    )


def _draft_snapshot(body: str) -> SimpleNamespace:
    return SimpleNamespace(
        summary_cn="测试摘要",
        body=body,
        tags_json=["开源项目"],
        evidence_json=[{"id": "source-1", "title": "示例项目", "url": "https://example.com/a", "metrics": {"stars_total": 100}}],
        source_name="GitHub Trending",
        source_url="https://example.com/a",
        title_options_json=["示例标题"],
        content_plan_json={},
        source_item=SimpleNamespace(title="示例项目", source_kind="github"),
    )


def _paragraphs(count: int = 4, repeat: int = 14) -> str:
    return "\n\n".join(f"　　这是第 {index} 段正文，说明项目背景、实现方式、适用边界与后续观察。" * repeat for index in range(1, count + 1))


class _FakeCompletion:
    def __init__(self, content: str) -> None:
        self.choices = [SimpleNamespace(message=SimpleNamespace(content=content))]


class _FakeClient:
    def __init__(self, content: str, captured: dict) -> None:
        self._content = content
        self._captured = captured
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self._captured["prompt"] = kwargs["messages"][0]["content"]
        return _FakeCompletion(self._content)


def _install_fake_client(monkeypatch, content: str, captured: dict) -> None:
    monkeypatch.setattr(auto_revision_module, "OpenAI", lambda **_kwargs: _FakeClient(content, captured))


def _review_agent_stub(captured: dict, *, score: int, issues: list[dict], search_queries: list[str] | None = None):
    """替身审核子 Agent：记录提示词并按给定结论返回。"""

    class FakeReviewAgent:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def review(self, prompt: str) -> ReviewResponse:
            captured["prompt"] = prompt
            return ReviewResponse(
                score=score,
                issues=[ReviewIssue(**item) for item in issues],
                summary="通过" if not issues else "需修改",
                search_queries=search_queries or [],
            )

    return FakeReviewAgent


def test_revision_prompt_states_length_floor_and_forbids_translationese(monkeypatch) -> None:
    captured: dict = {}
    payload = json.dumps({"summary_cn": "摘要", "body": _paragraphs(), "tags": ["开源项目"]}, ensure_ascii=False)
    _install_fake_client(monkeypatch, payload, captured)

    AutoRevisionTool(_settings(), SimpleNamespace()).invoke(
        "draft-1",
        {"failures": ["正文长度不足"]},
        {"issues": ["术语未解释"]},
        draft=_draft_snapshot(_paragraphs()),
    )

    prompt = captured["prompt"]
    assert "1200" in prompt and "4000" in prompt
    assert "不得低于下限" in prompt
    # 目标带明显低于硬上限，避免模型每次顶到上限附近。
    assert "1600 到 2200 个中文字符" in prompt
    assert "4 到 8 个自然段" in prompt
    # 明确禁止翻译腔，避免为了保守而写出生硬句子。
    assert "语境里看" in prompt and "不要用含糊措辞掩盖" in prompt


def test_revision_prompt_carries_web_search_evidence_when_provided(monkeypatch) -> None:
    """联网补充搭在改稿上：资料进入提示词，并限定只能用于名称与背景说明。"""
    captured: dict = {}
    payload = json.dumps({"summary_cn": "摘要", "body": _paragraphs(), "tags": ["开源项目"]}, ensure_ascii=False)
    _install_fake_client(monkeypatch, payload, captured)

    AutoRevisionTool(_settings(), SimpleNamespace()).invoke(
        "draft-1",
        {"failures": []},
        {"issues": ["第 2 段的 Antigravity 未说明是什么"]},
        draft=_draft_snapshot(_paragraphs()),
        search_evidence=[
            {"id": "exa-search-1", "title": "Exa 联网检索：Antigravity", "content": "Antigravity 是某公司的 agentic 开发平台。"}
        ],
    )

    prompt = captured["prompt"]
    assert "联网补充资料" in prompt
    assert "Antigravity 是某公司的 agentic 开发平台" in prompt
    assert "不得用于编造项目事实" in prompt
    assert "不要把正文写成安装教程" in prompt


def test_revision_prompt_stays_unchanged_without_search_evidence(monkeypatch) -> None:
    captured: dict = {}
    payload = json.dumps({"summary_cn": "摘要", "body": _paragraphs(), "tags": ["开源项目"]}, ensure_ascii=False)
    _install_fake_client(monkeypatch, payload, captured)

    AutoRevisionTool(_settings(), SimpleNamespace()).invoke(
        "draft-1", {"failures": []}, {"issues": ["术语未解释"]}, draft=_draft_snapshot(_paragraphs()),
    )

    assert "联网补充资料" not in captured["prompt"]


def test_review_response_normalizes_search_queries() -> None:
    response = ReviewResponse.model_validate(
        {
            "score": 88,
            "issues": [],
            "summary": "通过",
            "search_queries": ["Antigravity", " Antigravity ", "", "OpenCode", "Zed", "Cursor"],
        }
    )

    # 去空、去重，最多保留 2 条。
    assert response.search_queries == ["Antigravity", "OpenCode"]
    assert ReviewResponse.model_validate({"score": 90, "search_queries": "Antigravity"}).search_queries == ["Antigravity"]
    assert ReviewResponse.model_validate({"score": 90, "search_queries": None}).search_queries == []


def test_review_prompt_asks_for_names_that_need_explaining(monkeypatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(
        auto_review_module, "RestrictedContentTaskAgent", _review_agent_stub(captured, score=90, issues=[])
    )

    AutoReviewTool(_settings(), SimpleNamespace()).invoke("draft-1", draft=_draft_snapshot(_paragraphs()))

    prompt = captured["prompt"]
    assert "search_queries" in prompt
    # 只让审核模型挑“不解释就读不懂文章主体”的名称，避免把支持列表里的工具也拿去检索。
    assert "不解释就读不懂本文主体" in prompt
    assert "不要填" in prompt
    assert "最多 1 到 2 条" in prompt


def test_revision_reports_specific_article_shape_failure(monkeypatch) -> None:
    captured: dict = {}
    short_body = "\n\n".join("　　偏短的段落。" * 3 for _ in range(6))
    payload = json.dumps({"summary_cn": "摘要", "body": short_body, "tags": ["开源项目"]}, ensure_ascii=False)
    _install_fake_client(monkeypatch, payload, captured)

    with pytest.raises(AutoRevisionError) as caught:
        AutoRevisionTool(_settings(), SimpleNamespace()).invoke(
            "draft-1",
            {"failures": ["正文冗余"]},
            {},
            draft=_draft_snapshot(_paragraphs()),
        )

    # 失败原因必须指出具体约束，而不是笼统的“不符合结构”。
    assert "正文不符合要求" in str(caught.value)
    assert "1200" in str(caught.value)


def test_revision_payload_accepts_string_tags_and_paragraph_array_body() -> None:
    payload = RevisionPayload.model_validate(
        {"summary_cn": "摘要", "body": ["第一段", "第二段"], "tags": "开源项目, GitHub Trending"}
    )

    assert payload.body == "第一段\n\n第二段"
    assert payload.tags == ["开源项目", "GitHub Trending"]


def test_review_prompt_carries_source_evidence_and_spares_the_server_footer() -> None:
    """审核必须能看到来源证据，否则只能把每个具体细节都判成“缺少来源”。"""
    captured: dict = {}

    class FakeReviewAgent:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def review(self, prompt: str) -> ReviewResponse:
            captured["prompt"] = prompt
            return ReviewResponse(score=90, issues=[], summary="通过")

    import app.tools.auto_review as module

    original = module.RestrictedContentTaskAgent
    module.RestrictedContentTaskAgent = FakeReviewAgent
    try:
        snapshot = _draft_snapshot(_paragraphs())
        snapshot.evidence_json = [
            {
                "id": "source-1",
                "title": "affaan-m/ECC",
                "url": "https://github.com/affaan-m/ECC",
                "metrics": {"stars_total": 256737, "forks": 38420},
                "content": "仓库说明包含 68 个 agents、292 项 skills 与 npx ecc-universal@2.2.1 setup 安装命令。",
            }
        ]
        AutoReviewTool(_settings(), SimpleNamespace()).invoke("draft-1", draft=snapshot)
    finally:
        module.RestrictedContentTaskAgent = original

    prompt = captured["prompt"]
    # 证据原文与指标进入提示词，审核模型才能核对数字、数量与命令。
    assert "来源证据" in prompt
    assert "256737" in prompt
    assert "292 项 skills" in prompt
    assert "凡在此能找到的都属于有来源" in prompt
    # 服务端自动追加的文末提示行不得被当作缺陷。
    assert "由服务端自动添加" in prompt
    # 来源里没有的具体细节按 major 计，避免 critical 风暴。
    assert "按 major 计" in prompt


def test_review_prompt_states_evidence_missing_without_penalty() -> None:
    from app.tools.auto_review import _evidence_section

    assert "未保存" in _evidence_section(None)
    assert "未保存" in _evidence_section([])


def test_evidence_section_is_not_truncated_before_the_generation_budget() -> None:
    """审核必须看到与写作同一份证据：6000 字之后的证据不能被丢掉。"""
    from app.tools.auto_review import _evidence_section

    late_fact = "密钥需要自备：OpenAI API key 由使用者提供。"
    evidence = [
        {
            "id": "source-1",
            "title": "repo",
            "url": "https://github.com/o/r",
            "content": "填充内容。" * 1_500 + late_fact,
        }
    ]

    section = _evidence_section(evidence, limit=20_000)

    assert late_fact in section
    assert len(section) > 6_000


def test_evidence_section_marks_revision_search_entries() -> None:
    from app.tools.auto_review import _evidence_section

    section = _evidence_section(
        [{"id": "search-1", "title": "web", "url": "https://example.com", "content": "联网补充资料"}]
    )

    assert "search-N" in section
    assert "与来源证据同等可用" in section


def test_review_prompt_bounds_what_counts_as_a_defect(monkeypatch) -> None:
    captured: dict = {}

    class FakeReviewAgent:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def review(self, prompt: str) -> ReviewResponse:
            captured["prompt"] = prompt
            return ReviewResponse(score=90, issues=[], summary="通过")

    monkeypatch.setattr(auto_review_module, "RestrictedContentTaskAgent", FakeReviewAgent)

    result = AutoReviewTool(_settings(), SimpleNamespace()).invoke("draft-1", draft=_draft_snapshot(_paragraphs()))

    prompt = captured["prompt"]
    assert result.passed is True
    assert "判定边界" in prompt
    # 指标属于可用证据，不得判为缺少来源。
    assert "不得判为缺少来源" in prompt
    # 不得要求加入套话，也不得为保守而要求把句子改生硬。
    assert "套话" in prompt and "语言自然、直接优先于措辞谨慎" in prompt
    # 分数锚点：同一篇稿子的评分不应随风格偏好漂移。
    assert "打分锚点" in prompt
    assert "85 到 89 分" in prompt
    # 范围限定与可执行意见。
    assert "没做竞品对比" in prompt
    assert "必须可直接执行" in prompt
    # 不扣字眼：同义改写、同类补充、措辞偏好都不算缺陷；minor 上限收紧到 2 条（只用于语气与通顺）。
    assert "不扣字眼" in prompt
    assert "同义改写" in prompt
    assert "最多 2 条" in prompt
    # 开头必须交代主体与背景，无主语或从操作场景开头算 major。
    assert "开头是否在第一段交代了主体与背景" in prompt
    assert "无主语" in prompt


def test_generation_instruction_layers_structure_language_and_facts() -> None:
    instruction = _natural_article_instruction(1200, 3200)

    # 分层指令：结构、语言、取材重点、事实、输出都要在。
    for block in ("【结构】", "【语言】", "【取材与重点】", "【事实】", "【输出】"):
        assert block in instruction
    assert "4 到 8 个自然段" in instruction
    assert "语境里看" in instruction
    assert "不要用含糊措辞掩盖" in instruction
    assert "不得少于 1200 个中文字符" in instruction
    # 正向语感锚点：自然开头、允许短段、数字的中文可读写法；不鼓励谈论来源自身的缺失。
    assert "万能开头" in instruction
    assert "短段" in instruction
    assert "约 4.2 万" in instruction
    assert "README 里写明" not in instruction
    # 禁止用“来源没有说明”这类句子凑数。
    assert "禁止凑数" in instruction
    assert "来源没有说明" in instruction
    # 取材重点：先选 2–3 个要点展开，安装/价格/渠道/语言清单一律不写。
    assert "2 到 3 个要点" in instruction
    assert "安装步骤、价格与套餐、官方渠道清单、支持语言列表、命令与参数、版本号一律不写" in instruction
    # 开头必须交代主体与背景，禁止无主语的操作场景开头。
    assert "【开头】" in instruction
    assert "谁做的或来自哪里、它想解决什么麻烦" in instruction
    assert "每个句子都要有明确主语" in instruction
    assert "本地运行后，浏览器中会显示" in instruction
    # 长度给目标带而不是只给上下限，避免贴着下限写。
    assert "1600 到 2200 个中文字符" in instruction
    assert "不得超过 3200 个中文字符" in instruction
    assert "不要贴着下限写" in instruction
    # 禁止每篇同一结构。
    assert "不要每篇都套同一个顺序" in instruction


def test_generation_instruction_carries_the_sharing_persona() -> None:
    """真实反馈：文案语气太过严谨严肃。人设与反百科腔清单必须在提示词里。"""
    instruction = _natural_article_instruction(1200, 3200)

    assert "【人设与语气】" in instruction
    assert "分享者" in instruction
    # 分享口吻 ≠ 营销腔：叫卖词与烂梗仍然禁止。
    assert "绝了" in instruction and "家人们" in instruction
    assert "第一人称" in instruction
    # 反百科腔：点名要禁的句式 + 段末概括句。
    assert "【不要百科腔与研报腔】" in instruction
    assert "“X 是……的一个……”" in instruction
    assert "对读者来说" in instruction
    assert "这也让它成为……的入口" in instruction
    assert "只是复述本段" in instruction
    # 对照示例是模型最容易照做的一层。
    assert "【对照示例】" in instruction
    assert instruction.count("→") >= 3
    # 事实纪律没有被语气改写挤掉。
    assert "不得补充未经来源支持的" in instruction or "只能使用给定的证据包" in instruction


def test_generation_instruction_bans_padding_by_repetition() -> None:
    """实测发现：改掉百科腔后，模型改用“列举→逐条复述→总括句”凑字数，审核连报重复。"""
    instruction = _natural_article_instruction(1200, 3200)

    assert "尤其禁止这种凑字数的写法" in instruction
    assert "逐条复述" in instruction or "逐个复述" in instruction
    assert "每个要点只讲一次" in instruction
    assert "如果一段的最后一句能删掉而信息不减，就删掉它" in instruction
    # 实测发现的另两类来源细节问题：把采集时间当日期、描述来源里没有的界面文字。
    assert "都不是来源事实" in instruction
    assert "不要描述来源里没有的界面文字" in instruction
    assert "原样引用" in instruction


def test_review_and_revision_protect_the_sharing_tone() -> None:
    """审核与改稿不得把分享者口吻判成缺陷、也不得把它改回百科腔。"""
    from app.tools.auto_review import AutoReviewTool
    from app.tools.auto_revision import AutoRevisionTool

    review_source = inspect.getsource(AutoReviewTool.invoke)
    revision_source = inspect.getsource(AutoRevisionTool.invoke)

    assert "分享者口吻是目标文体" in review_source
    assert "属于 major 缺陷" in review_source
    assert "分享者" in revision_source
    assert "不要把文章改成百科定义句" in revision_source


def test_review_focuses_on_tone_and_fluency_not_details() -> None:
    """真实反馈：审核要主要改进语气与通顺，而不是过多关注细节。"""
    from app.tools.auto_review import AutoReviewTool
    from app.tools.auto_revision import AutoRevisionTool

    review_source = inspect.getsource(AutoReviewTool.invoke)

    assert "本次审核只看两件事" in review_source
    assert "**语气**" in review_source and "**通顺**" in review_source
    assert "都不属于本次审核范围" in review_source
    # 细节挑刺被明确压掉：minor 上限降到 2 条，且只用于语气与通顺。
    assert "最多 2 条" in review_source
    assert "不要为细节挑刺" in review_source
    # 打分锚点改成以语气与通顺为达标线。
    assert "语气是分享者口吻、语句通顺" in review_source
    # 改稿按同一优先级处理，不顺手重写全文。
    revision_source = inspect.getsource(AutoRevisionTool.invoke)
    assert "改写优先级" in revision_source


def test_evidence_packs_carry_metrics_for_the_review_model() -> None:
    item = NormalizedItem(
        source_kind=SourceKind.GITHUB,
        external_id="owner/repo",
        title="owner/repo",
        url="https://github.com/owner/repo",
        summary="示例摘要",
        content="示例正文",
        source_name="GitHub Trending",
        content_hash="a" * 64,
        category=ContentCategory.OPEN_SOURCE,
        metrics={"stars_total": 41987, "forks": 2373, "stars_period": 13164},
    )
    generator = OpenAICompatibleDraftGenerator.__new__(OpenAICompatibleDraftGenerator)
    generator.settings = SimpleNamespace(llm_evidence_max_chars=12000)

    baseline_pack = generator._evidence_pack(item)
    deterministic_pack = DeterministicDraftGenerator._evidence_pack(item)

    for pack in (baseline_pack, deterministic_pack):
        assert pack[0]["metrics"]["stars_total"] == 41987
        assert pack[0]["source_name"] == "GitHub Trending"
