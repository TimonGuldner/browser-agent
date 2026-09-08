from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', extra='ignore')

    supabase_url: str
    supabase_service_role_key: str
    llm_provider: str = 'openai'
    llm_model: str = 'gpt-4.1-mini'
    openai_api_key: str | None = None
    browser_use_api_key: str | None = None
    google_api_key: str | None = None
    anthropic_api_key: str | None = None
    headless: bool = True
    poll_seconds: float = 3.0
    max_steps: int = 50


settings = Settings()
