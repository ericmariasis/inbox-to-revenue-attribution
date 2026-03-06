from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_env: str = "local"
    database_url: str = "postgresql://localhost/attribution"
    jwt_secret: str = "replace_me"
    jwt_algorithm: str = "HS256"
    jwt_access_token_ttl_hours: int = 24
    magic_link_token_ttl_minutes: int = 15
    tracked_link_base_url: str = "https://trk.example.com"


@lru_cache
def get_settings() -> Settings:
    return Settings()
