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
