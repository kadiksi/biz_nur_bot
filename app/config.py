from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )

    database_url: str = f"sqlite+aiosqlite:///{PROJECT_ROOT / 'data' / 'biz.db'}"
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
    site_port: int = 8000
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


settings = Settings()
