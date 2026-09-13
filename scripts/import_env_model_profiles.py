"""将当前环境变量中的任务模型配置导入系统设置页。

仅用于用户明确发起的一次性迁移；不调用模型，输出中不包含 Base URL 或密钥。
"""

from __future__ import annotations

from app.config import Settings
from app.services.runtime_settings import decrypt_api_key, encrypt_api_key
from app.storage.database import SessionLocal
from app.storage.repositories import ContentRepository
from app.storage.tables import ModelProfileRow


TASKS = (
    "conversation",
    "content",
    "review",
    "illustration_planner",
    "evidence_selector",
)


def normalize_profile_names(repository: ContentRepository, profiles: list[ModelProfileRow]) -> int:
    """旧版导入名仅作内部兼容；统一为模型名，页面不再使用独立别名。"""
    renamed, used = 0, set()
    for row in sorted(profiles, key=lambda item: item.id):
        base_name, candidate, suffix = row.model_name.strip(), row.model_name.strip(), 2
        while candidate in used:
            candidate = f"{base_name} · {suffix}"
            suffix += 1
        used.add(candidate)
        if row.name != candidate:
            row.name = candidate
            renamed += 1
            repository.add_runtime_setting_audit(
                "model_profile", row.id, "已导入别名", "已规范为模型名",
                changed_by="模型配置整理",
            )
    repository.session.flush()
    return renamed


def main() -> None:
    settings = Settings()
    created_for_tasks: list[str] = []
    assigned_tasks: list[str] = []
    skipped_incomplete_tasks: list[str] = []
    with SessionLocal() as session:
        repository = ContentRepository(session)
        profiles = repository.list_model_profiles()
        by_signature = {}
        for row in profiles:
            api_key = decrypt_api_key(settings, row.encrypted_api_key)
            if api_key:
                by_signature[(row.model_name, row.base_url, api_key)] = row
        existing_names = {item.name for item in profiles}
        for task in TASKS:
            model = settings.model_for(task)
            base_url = settings.base_url_for(task)
            api_key = settings.api_key_for(task)
            if not (model and base_url and api_key):
                skipped_incomplete_tasks.append(task)
                continue
            signature = (model.strip(), base_url.strip(), api_key.strip())
            row = by_signature.get(signature)
            if row is None:
                base_name = signature[0]
                name, suffix = base_name, 2
                while name in existing_names:
                    name = f"{base_name} {suffix}"
                    suffix += 1
                row = repository.save_model_profile(
                    profile_id=None,
                    name=name,
                    model_name=signature[0],
                    base_url=signature[1],
                    encrypted_api_key=encrypt_api_key(settings, signature[2]),
                    replace_api_key=True,
                )
                profiles.append(row)
                existing_names.add(name)
                by_signature[signature] = row
                created_for_tasks.append(task)
            setting_key = f"runtime.model.{task}.profile_id"
            previous = repository.get_app_setting(setting_key)
            repository.set_app_setting(setting_key, row.id, updated_by="环境变量导入")
            repository.add_runtime_setting_audit(
                "model_assignment", task, previous or "环境变量", row.id,
                changed_by="环境变量导入",
            )
            assigned_tasks.append(task)
        renamed_profiles = normalize_profile_names(repository, profiles)
        session.commit()
    print({
        "created_profile_for_tasks": created_for_tasks,
        "assigned_tasks": assigned_tasks,
        "skipped_incomplete_tasks": skipped_incomplete_tasks,
        "profile_count": len(profiles),
        "renamed_profiles": renamed_profiles,
    })


if __name__ == "__main__":
    main()
