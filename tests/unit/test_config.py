import pytest
from pydantic import ValidationError

from app.config import Settings

# model_validate checks raw input exactly as it would arrive from the environment, without
# reading the real environment or a .env file.


def test_production_requires_an_admin_token():
    with pytest.raises(ValidationError, match="ADMIN_TOKEN"):
        Settings.model_validate({"environment": "production"})


def test_short_admin_token_is_rejected():
    with pytest.raises(ValidationError, match="admin_token"):
        Settings.model_validate({"admin_token": "short"})


def test_local_runs_without_an_admin_token():
    assert Settings.model_validate({"environment": "local"}).admin_token is None


def test_log_level_is_case_insensitive():
    assert Settings.model_validate({"log_level": "debug"}).log_level == "DEBUG"


def test_production_requires_a_bank_callback_secret():
    with pytest.raises(ValidationError, match="BANK_CALLBACK_SECRET"):
        Settings.model_validate({"environment": "production", "admin_token": "x" * 16})


def test_short_bank_callback_secret_is_rejected():
    with pytest.raises(ValidationError, match="bank_callback_secret"):
        Settings.model_validate({"bank_callback_secret": "short"})


def test_private_webhook_urls_are_refused_in_production():
    with pytest.raises(ValidationError, match="WEBHOOK_ALLOW_PRIVATE_URLS"):
        Settings.model_validate(
            {
                "environment": "production",
                "admin_token": "x" * 16,
                "bank_callback_secret": "y" * 16,
                "webhook_allow_private_urls": True,
            }
        )
