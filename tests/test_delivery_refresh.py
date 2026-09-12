"""投递后再修改：标记“投递已过期”，重新投递时原地覆盖远端草稿。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services import auto_delivery


class _Job:
    def __init__(self, *, media_id: str | None, cover_media_id: str | None = "cover-media") -> None:
        self.id = "job-1"
        self.draft_id = "draft-1"
        self.wechat_draft_media_id = media_id
        self.cover_media_id = cover_media_id
        self.inline_asset_ids_json = ["inline-1"]
        self.inline_image_urls_json = [{"url": "https://mmbiz.example/1.png", "after_paragraph": 2}]
        self.state = "draft_created" if media_id else "assets_selected"
        self.error_message = None


class _Repository:
    def __init__(self, job: _Job) -> None:
        self.job = job
        self.states: list[tuple[str, str]] = []
        self.created: list[str] = []
        self.updated: list[str] = []

    def get_draft(self, draft_id: str):
        return SimpleNamespace(
            id=draft_id, status="ready_to_publish", body="　　正文。",
            title_options_json=["标题"], summary_cn="摘要", source_url="https://github.com/o/r",
        )

    def get_wechat_publication_for_draft(self, draft_id: str):
        return self.job

    def mark_wechat_status(self, job_id: str, state: str, error_message: str = "") -> None:
        self.states.append((state, error_message))

    def mark_wechat_draft_created(self, job_id: str, media_id: str):
        self.created.append(media_id)
        self.job.wechat_draft_media_id = media_id
        self.job.state = "draft_created"
        return self.job

    def mark_wechat_draft_updated(self, job_id: str):
        self.updated.append(job_id)
        self.job.state = "draft_created"
        return self.job


def _patch_wechat(monkeypatch, *, update_fails: bool = False, create_fails: bool = False):
    calls: dict[str, list] = {"update": [], "create": []}

    class _Client:
        def __init__(self, _settings) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc) -> None:
            return None

        async def update_draft(self, **kwargs):
            if update_fails:
                raise auto_delivery.WechatOfficialAccountError("远端草稿不存在")
            calls["update"].append(kwargs)

        async def create_draft(self, **kwargs):
            if create_fails:
                raise auto_delivery.WechatOfficialAccountError("创建失败")
            calls["create"].append(kwargs)
            return "new-media-id"

    monkeypatch.setattr(auto_delivery, "WechatOfficialAccountTool", _Client)
    monkeypatch.setattr(auto_delivery, "render_wechat_html", lambda body, urls: "<p>渲染</p>")
    monkeypatch.setattr(auto_delivery, "normalize_wechat_description", lambda value: "摘要")
    return calls


@pytest.mark.asyncio
async def test_existing_remote_draft_is_overwritten_in_place(monkeypatch: pytest.MonkeyPatch) -> None:
    repository = _Repository(_Job(media_id="existing-media-id"))
    calls = _patch_wechat(monkeypatch)

    job = await auto_delivery.retry_agent_selected_wechat_draft(SimpleNamespace(), repository, "draft-1")

    assert calls["update"] and calls["update"][0]["media_id"] == "existing-media-id"
    assert calls["create"] == []  # 不新建，避免留下孤儿草稿
    assert repository.updated == ["job-1"]
    assert job.state == "draft_created"


@pytest.mark.asyncio
async def test_missing_remote_draft_falls_back_to_create(monkeypatch: pytest.MonkeyPatch) -> None:
    repository = _Repository(_Job(media_id="deleted-media-id"))
    calls = _patch_wechat(monkeypatch, update_fails=True)

    job = await auto_delivery.retry_agent_selected_wechat_draft(SimpleNamespace(), repository, "draft-1")

    assert calls["create"], "覆盖失败时应退化为新建"
    assert repository.created == ["new-media-id"]
    assert job.wechat_draft_media_id == "new-media-id"


@pytest.mark.asyncio
async def test_first_delivery_still_creates_draft(monkeypatch: pytest.MonkeyPatch) -> None:
    repository = _Repository(_Job(media_id=None))
    calls = _patch_wechat(monkeypatch)

    await auto_delivery.retry_agent_selected_wechat_draft(SimpleNamespace(), repository, "draft-1")

    assert calls["create"] and not calls["update"]
    assert repository.created == ["new-media-id"]


def test_stale_delivery_allows_new_selection_and_keeps_media_id() -> None:
    """已投递草稿被改后：允许重新选择图片，并保留 media_id 以便原地覆盖。

    真实故障：`save_wechat_asset_selection` 见到 media_id 就提前返回，新选择被静默丢弃，
    投递时报“已选择的封面图已删除或不可用”；`save_wechat_publication` 又只允许
    `ready_to_publish`，已投递的草稿根本无法刷新素材。
    """
    source = __import__("pathlib").Path("app/storage/repositories.py").read_text(encoding="utf-8")

    assert 'row.state != "delivery_stale"' in source
    assert "refreshing_existing_draft" in source
    assert "ReviewStatus.DRAFTBOX_CREATED.value," in source


def test_publication_stale_marking_keeps_media_id_and_clears_selection() -> None:
    """已投递的草稿被修改后：保留 media_id、清空图片选择、标记 delivery_stale。"""
    source = (__import__("pathlib").Path("app/storage/repositories.py").read_text(encoding="utf-8"))

    assert "def mark_wechat_publication_stale" in source
    assert '"delivery_stale"' in source
    # 已投递时不再“永远不改动”，而是标记过期以便下次覆盖。
    assert "return self.mark_wechat_publication_stale(draft_id, reason)" in source
