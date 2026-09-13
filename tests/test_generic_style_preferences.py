"""通用长期偏好：不写新代码也能加“别的偏好”。

用户提问：“以后有不是尾图的其他偏好怎么办呢？”
答案分两层：
- **文字层面**的偏好（措辞、结构、禁忌、必须包含什么）→ 存进 `publication.style_notes`，
  每次生成／改稿／审核都会作为规则块注入提示词，因此加偏好不需要改代码；
- **流程层面**的新能力（例如“自动加一张数据表”）→ 必须有人写代码，工具会明确说明，
  而不是静默不生效。
"""

from __future__ import annotations

import inspect
import json

from app.agent_tools import publication_preferences as tools_module
from app.services import generator as generator_module
from app.services.publication_preferences import (
    add_style_note,
    load_style_notes,
    preference_rules_block,
    remove_style_note,
)
from app.tools import auto_review, auto_revision


class _Repository:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get_app_setting(self, key: str, default: str | None = None) -> str | None:
        return self.values.get(key, default)

    def set_app_setting(self, key: str, value: str, *, updated_by: str = "agent"):
        self.values[key] = value
        return type("Row", (), {"key": key, "value": value})()


def test_style_notes_round_trip_and_dedupe() -> None:
    repository = _Repository()

    add_style_note(repository, "正文不要用问句做小标题")
    add_style_note(repository, "正文不要用问句做小标题")  # 重复不重复存
    notes = add_style_note(repository, "每篇文末加一行：本文由 AI 整理")

    assert notes == ("正文不要用问句做小标题", "每篇文末加一行：本文由 AI 整理")
    assert load_style_notes(repository) == notes
    assert json.loads(repository.values["publication.style_notes"]) == list(notes)

    assert remove_style_note(repository, "正文不要用问句做小标题") == ("每篇文末加一行：本文由 AI 整理",)


def test_rules_block_is_empty_without_notes_and_renders_otherwise() -> None:
    repository = _Repository()

    assert preference_rules_block(repository) == ""

    add_style_note(repository, "语气克制，不要感叹句")
    block = preference_rules_block(repository)

    assert "运营者的长期偏好" in block
    assert "- 语气克制，不要感叹句" in block


def test_rules_block_is_injected_into_generation_review_and_revision() -> None:
    """同一套偏好必须同时进生成、审核、改稿，否则会出现“改对了又被判错”。"""
    assert "preference_rules_block" in inspect.getsource(generator_module)
    assert "_preference_rules(self.repository)" in inspect.getsource(auto_review.AutoReviewTool.invoke)
    assert "_preference_rules(self.repository)" in inspect.getsource(auto_revision.AutoRevisionTool.invoke)


def test_generic_preference_tools_exist_with_sync_entrypoints() -> None:
    tools = {tool.name: tool for tool in tools_module.build_publication_preference_tools("session-1")}

    assert {"show_publication_preferences", "add_style_preference", "remove_style_preference"} <= set(tools)
    for tool in tools.values():
        assert callable(getattr(tool, "func", None))


def test_unknown_preference_is_reported_not_silently_ignored() -> None:
    """流程层面没有的能力：拒绝并说明，而不是假装记住了。"""
    source = inspect.getsource(tools_module)

    assert "请给出这条长期偏好的内容" in source  # 空输入被拒
    assert "对已生成的草稿需要重新生成或重写才会带上" in source  # 讲清生效范围
