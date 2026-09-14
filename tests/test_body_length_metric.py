"""正文长度口径唯一化 + 字数配置真的能被改。

两个真实困惑（2026-09-14 用户实测）：

1. **“我让 agent 改字数上限，但是配置页并没有改”**——当时 Agent 手里只有“长期写作偏好”，
   于是它把“上限 2800”记成了一句偏好（`publication.style_notes`），配置页那两个数字当然不动。
   写作偏好只是提示词里的一句话，和真正的配置是两回事；现在有了 `set_generation_settings`。
2. **同一篇稿子被数成四个数**：`len(body)` 2628、规则审核 2614（含段首缩进）、
   成形校验 2483（去空白）、提示词说的“中文字符”1657，而编辑器显示 1957——于是同一篇稿子
   一个说太短、一个说太长。现在全链路只用 `body_char_count()`。
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from app.services.plain_text import (
    SOURCE_FOOTER_PREFIXES,
    article_length_band,
    body_char_count,
    body_source_lines_removed,
    compose_natural_article,
)


FOOTER = "点击查看原文跳转项目地址"


def test_source_footer_prefixes_stay_public() -> None:
    """渲染、审核、前端预览共用同一套来源行判定，常量必须保持公开。"""
    assert FOOTER in SOURCE_FOOTER_PREFIXES


def test_canonical_count_ignores_indent_spaces_and_footer_but_counts_latin() -> None:
    body = f"　　OpenAI 把 Codex 插件摊开了。\n\n　　第二段也有内容。\n\n{FOOTER}"

    # 段首两个全角空格、换行、来源尾注都不算；Latin 字母算，各算一个。
    assert body_char_count(body) == len("OpenAI把Codex插件摊开了。第二段也有内容。")


def test_footer_removal_is_shared_by_both_paths() -> None:
    with_footer = f"　　第一段。\n\n　　第二段。\n\n{FOOTER}"
    without_footer = "　　第一段。\n\n　　第二段。"

    assert body_char_count(with_footer) == body_char_count(without_footer)
    assert body_source_lines_removed(with_footer).strip().endswith("第二段。")


def test_formation_check_uses_the_same_metric() -> None:
    """成形校验过去用 `len(re.sub(r"\\s+", "", body))`，与审核口径差一个来源尾注。"""
    body = "\n\n".join([f"　　这是第 {index} 段正文，用来占位。" for index in range(1, 5)])
    _, shape = compose_natural_article(body, min_chars=10)

    assert shape["body_chars"] == body_char_count(body)


def test_rule_review_reports_the_canonical_count() -> None:
    from app.tools.auto_review import rule_review

    body = "\n\n".join([f"　　这是第 {index} 段正文，用来占位。" for index in range(1, 5)])
    draft = SimpleNamespace(
        body=f"{body}\n\n{FOOTER}",
        source_url="https://example.com/source",
        source_name="Example",
        title_options_json=["标题"],
        tags_json=["标签"],
        source_item=SimpleNamespace(source_kind="github"),
        content_plan_json={},
    )

    rules = rule_review(draft, 10, 100000)

    assert rules["body_chars"] == body_char_count(draft.body)


def test_generation_and_revision_prompts_state_the_same_metric() -> None:
    """提示词不能再写“中文字符”：校验数的是含英文与标点的全部字符。"""
    from app.services import generator
    from app.tools import auto_revision

    instruction = inspect.getsource(generator._natural_article_instruction)
    revision = inspect.getsource(auto_revision.AutoRevisionTool.invoke)

    assert "去掉空白与来源尾注后的全部字符" in instruction
    assert "中文字符" not in instruction
    assert "中文字符" not in revision


def test_band_is_derived_from_the_same_units() -> None:
    assert article_length_band(1600, 2200) == (1600, 2200, 1400, 2400)
    # 用户这次要的“上限 2800”：硬上限跟着放宽 200。
    assert article_length_band(1600, 2800) == (1600, 2800, 1400, 3000)


class _Repository:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def set_app_setting(self, key: str, value: str, *, updated_by: str = "agent") -> None:
        self.values[key] = value

    def get_app_setting(self, key: str, default=None):  # noqa: ANN001
        return self.values.get(key, default)

    def list_app_settings(self) -> dict[str, str]:
        return dict(self.values)


class _FakeSession:
    def __init__(self, repository: _Repository) -> None:
        self.repository = repository

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def commit(self) -> None:
        return None


def _patch_settings(monkeypatch: pytest.MonkeyPatch, repository: _Repository) -> None:
    """让“读取生效设置”走假仓储：写入后重新读取要能看到新值。"""
    from app.agent_tools import publication_preferences as prefs
    from app.services import runtime_settings

    def fake_load(_settings):  # noqa: ANN001
        return SimpleNamespace(
            draft_body_min_chars=int(repository.values.get("runtime.draft_body_min_chars", 1600)),
            draft_body_max_chars=int(repository.values.get("runtime.draft_body_max_chars", 2200)),
        )

    monkeypatch.setattr(prefs, "SessionLocal", lambda: _FakeSession(repository))
    monkeypatch.setattr(prefs, "ContentRepository", lambda _session: repository)
    monkeypatch.setattr(prefs, "get_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(runtime_settings, "load_runtime_settings", fake_load)


def test_agent_can_actually_change_the_configured_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """“把上限改成 2800”必须写进配置，而不是记成写作偏好。"""
    from app.agent_tools import publication_preferences as prefs

    repository = _Repository()
    _patch_settings(monkeypatch, repository)

    tools = {item.name: item for item in prefs.build_publication_preference_tools("session-1")}
    result = tools["set_generation_settings"].invoke({"target_max_chars": 2800})

    assert result["status"] == "done"
    assert repository.values["runtime.draft_body_max_chars"] == "2800"
    assert result["hard_max_chars"] == 3000, "硬上限随目标上限放宽 200"
    assert "配置页同一处数字" in result["message"]


def test_invalid_target_is_rejected_with_the_config_page_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.agent_tools import publication_preferences as prefs

    repository = _Repository()
    _patch_settings(monkeypatch, repository)

    tools = {item.name: item for item in prefs.build_publication_preference_tools("session-1")}
    result = tools["set_generation_settings"].invoke({"target_max_chars": 9999})

    assert result["status"] == "rejected"
    assert "不得超过" in result["reason"]
    assert repository.values == {}


def test_show_generation_settings_reports_the_effective_band(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.agent_tools import publication_preferences as prefs

    repository = _Repository()
    repository.values["runtime.draft_body_max_chars"] = "2800"
    _patch_settings(monkeypatch, repository)

    tools = {item.name: item for item in prefs.build_publication_preference_tools("session-1")}
    result = tools["show_generation_settings"].invoke({})

    assert result["target_max_chars"] == 2800
    assert result["hard_max_chars"] == 3000
    assert "去掉空白与来源尾注" in result["metric"]
