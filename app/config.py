import os
from pathlib import Path

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )

    # Папка для базы, моделей распознавания и ключей сервисов. На Railway — точка монтирования тома.
    data_dir: Path = PROJECT_ROOT / "data"
    database_url: str | None = None    # по умолчанию SQLite в data_dir/biz.db
    # Куда сайт дописывает ключи сервисов и подключения 1С. По умолчанию .env проекта,
    # на Railway — data_dir/secrets.env (файловая система контейнера стирается при деплое)
    secrets_file: Path | None = None
    owner_telegram_id: int | None = None
    timezone: str = "Europe/Moscow"
    sync_stale_hours: int = 24

    anthropic_api_key: str | None = None
    model_main: str = "claude-opus-5-5"
    model_departments: str = "claude-sonnet-5-5"
    main_effort: str = "medium"        # low | medium | high | xhigh | max
    claude_fallbacks: bool = True      # серверный fallback при отказе модели (beta)
    claude_web_tools: bool = True      # поиск и чтение страниц в интернете (документация сервисов)

    # Сайт: слушаем все интерфейсы, чтобы открывался с телефона в той же сети
    site_host: str = "0.0.0.0"
    site_port: int = Field(8000, validation_alias=AliasChoices("SITE_PORT", "PORT"))  # PORT — от хостинга
    site_url: str | None = None        # если пусто — http://<IP этого компьютера в сети>:site_port

    # 1С: подключения — строки ONEC_<ИМЯ>_URL/_USER/_PASSWORD в .env (см. app/integrations/onec.py)
    onec_timeout: float = 60           # секунд на один запрос к 1С
    onec_sync_minutes: int = 60        # как часто подтягивать факты; 0 — только по просьбе боту

    speech_language: str = "auto"      # язык голосовых: auto, ru, kk… (распознавание на этом компьютере)

    bot_token_main: str | None = None
    bot_token_sales: str | None = None
    bot_token_marketing: str | None = None
    bot_token_warehouse: str | None = None
    bot_token_finance: str | None = None

    @model_validator(mode="after")
    def _defaults(self):
        self.data_dir = Path(self.data_dir)
        if not self.database_url:
            self.database_url = f"sqlite+aiosqlite:///{self.data_dir / 'biz.db'}"
        if self.secrets_file is None:
            self.secrets_file = self.data_dir / "secrets.env" if os.environ.get("RAILWAY_ENVIRONMENT") else PROJECT_ROOT / ".env"
        if not self.site_url and os.environ.get("RAILWAY_PUBLIC_DOMAIN"):
            self.site_url = f"https://{os.environ['RAILWAY_PUBLIC_DOMAIN']}"
        return self


settings = Settings()
