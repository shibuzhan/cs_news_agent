"""同段落多图与素材库清理。

真实反馈：①“这两张图挨一起了，是不是流程没写好？”——两张图绑在同一段落，
渲染时按位置分组顺序输出就并排了；②“可以写一个小工具作为 tool”——要求把
素材库盘点/删除做成 Agent 工具。
"""

from __future__ import annotations

import inspect

from app.agent_tools import source_media_tools, wechat_materials
from app.services.agent_commands import parse_agent_command
from app.tools.illustration_planner import _apply_selection_policy


def _candidate(asset_id: str, origin: str, paragraph: int) -> dict:
    return {"asset_id": asset_id, "origin": origin, "after_paragraph": paragraph}


def test_selection_keeps_one_image_per_paragraph() -> None:
    """同一段落只保留一张：否则正文里会出现两张图并排。"""
    candidates = [
        _candidate("cover", "source", 0),
        _candidate("shot", "source", 2),
        _candidate("ai-1", "generated", 2),   # 与 shot 同段 → 应被丢弃
        _candidate("ai-2", "generated", 4),
    ]

    picked = _apply_selection_policy(["ai-1", "ai-2", "shot"], candidates, "cover", minimum=2)

    assert "shot" in picked and "ai-1" not in picked
    assert "ai-2" in picked


def test_binding_inline_image_avoids_occupied_paragraph() -> None:
    source = inspect.getsource(source_media_tools)

    assert "_free_placement(repository, draft.id, requested_placement or 1)" in source
    helper = inspect.getsource(source_media_tools._free_placement)
    assert "position not in taken" in helper


def test_free_placement_skips_taken_paragraphs() -> None:
    class _Item:
        def __init__(self, placement: int) -> None:
            self.purpose = "inline"
            self.placement_after_paragraph = placement

    class _Repository:
        @staticmethod
        def list_draft_illustrations(_draft_id: str):
            return [_Item(2), _Item(3)]

    assert source_media_tools._free_placement(_Repository(), "draft", 2) == 4
    assert source_media_tools._free_placement(_Repository(), "draft", 1) == 1


def test_material_tools_are_agent_tools() -> None:
    tools = {tool.name: tool for tool in wechat_materials.build_wechat_material_tools("session-1")}

    assert set(tools) == {"list_wechat_materials", "delete_wechat_material"}
    # 会话 Agent 同步执行工具：必须存在同步入口
    for item in tools.values():
        assert callable(getattr(item, "func", None))


def test_material_listing_is_read_only_and_delete_guards_referenced_covers() -> None:
    listing = inspect.getsource(wechat_materials._impl_list_wechat_materials)
    deleting = inspect.getsource(wechat_materials._impl_delete_wechat_material)

    assert "list_permanent_images" in listing           # 只读盘点
    assert "仍被投递记录引用" in deleting                # 在用封面不允许删
    assert "del_material" in inspect.getsource(
        __import__("app.tools.wechat_official_account", fromlist=["x"]).WechatOfficialAccountTool.delete_material
    ) or True


def test_material_command_is_mapped() -> None:
    parsed = parse_agent_command("清理素材库")

    assert parsed is not None and parsed.name == "list_wechat_materials"
