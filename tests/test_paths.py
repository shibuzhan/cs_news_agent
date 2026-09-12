"""项目资源目录解析：容器内从 site-packages 导入时也必须找到 agent_skills。"""

from __future__ import annotations

import app.paths as paths
from app.paths import ASSET_DIR_ENV, agent_skills_dir, resolve_asset


def test_agent_skills_dir_resolves_with_entry_and_reference_files() -> None:
    skills = agent_skills_dir()

    assert skills is not None
    assert (skills / "github-content-writing" / "SKILL.md").is_file()
    assert (skills / "github-content-writing" / "references" / "evidence-and-search.md").is_file()


def test_resolve_asset_finds_the_wechat_skill_script() -> None:
    script = resolve_asset("wechat-official-account/scripts/wechat_official_api.py")

    assert script is not None
    assert script.is_file()


def test_env_override_takes_priority(monkeypatch) -> None:
    monkeypatch.setenv(ASSET_DIR_ENV, str(paths.Path.cwd()))
    paths._asset_root.cache_clear()
    try:
        assert agent_skills_dir() is not None
    finally:
        paths._asset_root.cache_clear()


def test_cwd_candidate_covers_container_site_packages_import(monkeypatch) -> None:
    """容器内 `import app` 命中 site-packages 时包旁没有 agent_skills，必须回退到 CWD（/app）。"""
    monkeypatch.setattr(
        paths, "_candidate_roots", lambda: [paths.Path("site-packages-like"), paths.Path.cwd()]
    )
    paths._asset_root.cache_clear()
    try:
        skills = agent_skills_dir()
        assert skills is not None
        assert skills.parent == paths.Path.cwd()
    finally:
        paths._asset_root.cache_clear()


def test_missing_asset_root_degrades_to_none(monkeypatch) -> None:
    monkeypatch.setattr(paths, "_candidate_roots", lambda: [paths.Path("definitely-missing-dir")])
    paths._asset_root.cache_clear()
    try:
        assert agent_skills_dir() is None
        assert resolve_asset("wechat-official-account/scripts/wechat_official_api.py") is None
    finally:
        paths._asset_root.cache_clear()
