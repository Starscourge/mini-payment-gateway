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
