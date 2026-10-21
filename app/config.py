from functools import lru_cache
from typing import Annotated, Literal, Self

from pydantic import BeforeValidator, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _upper(value: object) -> object:
    return value.upper() if isinstance(value, str) else value


class Settings(BaseSettings):
    """Runtime configuration, read from environment variables (and from `.env` locally)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: Literal["local", "test", "production"] = "local"
    database_url: str = "postgresql+asyncpg://gateway:gateway@localhost:5432/gateway"
    redis_url: str = "redis://localhost:6379/0"
    # Protects POST /v1/merchants. Optional locally, required in production.
    admin_token: SecretStr | None = Field(default=None, min_length=16)
    # Where the worker submits payments (the mock bank).
    bank_url: str = "http://localhost:8001"
    # Shared with the bank, which signs its callbacks with it. While unset, every callback is
    # rejected; required in production.
    bank_callback_secret: SecretStr | None = Field(default=None, min_length=16)
    payment_ttl_minutes: int = Field(default=15, ge=1, le=24 * 60)
    log_level: Annotated[Literal["DEBUG", "INFO", "WARNING", "ERROR"], BeforeValidator(_upper)] = (
        "INFO"
    )
    log_format: Literal["json", "console"] = "json"

    @model_validator(mode="after")
    def _secrets_required_in_production(self) -> Self:
        if self.environment == "production":
            if self.admin_token is None:
                raise ValueError("ADMIN_TOKEN must be set when ENVIRONMENT=production")
            if self.bank_callback_secret is None:
                raise ValueError("BANK_CALLBACK_SECRET must be set when ENVIRONMENT=production")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
