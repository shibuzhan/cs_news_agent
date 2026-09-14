from cryptography.fernet import Fernet

from app.config import Settings
from app.services.runtime_settings import (
    RuntimeSettingsError,
    decrypt_api_key,
    encrypt_api_key,
    validate_runtime_options,
)


def test_model_profile_secret_round_trip_does_not_change_plaintext() -> None:
    settings = Settings(model_profile_encryption_key=Fernet.generate_key().decode())
    encrypted = encrypt_api_key(settings, "secret-for-test")
    assert encrypted != "secret-for-test"
    assert decrypt_api_key(settings, encrypted) == "secret-for-test"


def test_runtime_settings_reject_unconfirmed_image_ratio() -> None:
    try:
        validate_runtime_options({"image_generation_ratio": "16:9"})
    except RuntimeSettingsError as exc:
        assert "4:3" in str(exc)
    else:
        raise AssertionError("unconfirmed ratio should not be accepted")


def test_runtime_settings_accepts_agnes_reference_candidates() -> None:
    assert validate_runtime_options({"image_generation_size": "2K", "image_generation_ratio": "4:3"}) == {
        "image_generation_size": "2K",
        "image_generation_ratio": "4:3",
    }


def test_body_length_settings_are_targets_with_a_200_char_hard_margin() -> None:
    """配置页填的是目标值；硬区间＝目标上下各放宽 200 字（用户明确要求的口径）。"""
    from app.services.plain_text import (
        HARD_BAND_MARGIN,
        MAX_TARGET_CHARS,
        MIN_HARD_BODY_CHARS,
        NATURAL_ARTICLE_MIN_CHARS,
        article_length_band,
    )

    assert HARD_BAND_MARGIN == 200
    assert article_length_band(1600, 2200) == (1600, 2200, 1400, 2400)
    # 目标下限有底线：填得再小也不会低于 NATURAL_ARTICLE_MIN_CHARS，硬下限＝底线再放宽 200。
    assert article_length_band(100, 1200) == (
        NATURAL_ARTICLE_MIN_CHARS,
        1200,
        NATURAL_ARTICLE_MIN_CHARS - HARD_BAND_MARGIN,
        1400,
    )
    # 目标上限不得超过绝对上限；上下限颠倒时按“下限”为准，不产生反向区间。
    assert article_length_band(3000, 99999)[1] == MAX_TARGET_CHARS
    assert article_length_band(2000, 1500) == (2000, 2000, 1800, 2200)


def test_body_length_settings_validation_messages() -> None:
    # 目标区间颠倒、目标下限低到没意义、目标上限过大都要能明确报错（不再静默抬高）。
    for payload, fragment in (
        ({"draft_body_min_chars": 2600, "draft_body_max_chars": 2200}, "不能大于"),
        ({"draft_body_min_chars": 500}, "不得低于 800"),
        ({"draft_body_max_chars": 9000}, "不得超过 6000"),
    ):
        try:
            validate_runtime_options(payload)
        except RuntimeSettingsError as exc:
            assert fragment in str(exc), (payload, str(exc))
        else:
            raise AssertionError(f"应当拒绝：{payload}")

    # 合法值按 app_settings 的存储形式（字符串）返回。
    assert validate_runtime_options({"draft_body_min_chars": 1600, "draft_body_max_chars": 2200}) == {
        "draft_body_min_chars": "1600",
        "draft_body_max_chars": "2200",
    }
