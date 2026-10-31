from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)
    environment: Literal["local", "test", "production"] = "local"
    database_url: SecretStr = SecretStr(
        "postgresql+psycopg://kefu_exec:local-exec@127.0.0.1:55432/kefu"
    )
    read_database_url: SecretStr = SecretStr(
        "postgresql+psycopg://kefu_read:local-read@127.0.0.1:55432/kefu"
    )
    work_database_url: SecretStr = SecretStr(
        "postgresql+psycopg://kefu_work:local-work@127.0.0.1:55432/kefu"
    )
    migration_database_url: SecretStr | None = None
    import_database_url: SecretStr | None = None
    redis_url: SecretStr = SecretStr("redis://127.0.0.1:56379/0")
    app_origin: str = "http://localhost:5173"
    secure_cookies: bool = False
    simulated_business: bool = True
    model_mode: Literal["demo", "qwen"] = "demo"
    model_provider: Literal["dashscope", "ollama"] = "dashscope"
    model_base_url: str = Field(
        default="https://dashscope.aliyuncs.com/compatible-mode/v1",
        validation_alias=AliasChoices("MODEL_BASE_URL", "OPENAI_BASE_URL"),
    )
    model_api_key: SecretStr = Field(
        default=SecretStr(""), validation_alias=AliasChoices("MODEL_API_KEY", "OPENAI_API_KEY")
    )
    model_name: str = Field(
        default="qwen3.5-35b-a3b", validation_alias=AliasChoices("MODEL_NAME", "OPENAI_MODEL")
    )
    model_revision: str = "provider-unversioned"
    embedding_model_path: Path | None = None
    embedding_provider: Literal["ollama", "sentence_transformers"] | None = None
    embedding_model_name: str = "qwen3-embedding:0.6b"
    embedding_base_url: str = "http://127.0.0.1:11434"
    embedding_manifest_path: Path | None = None
    embedding_revision: str = ""
    materials_root: Path = Path("data/runtime/materials")
    context_root: Path = Path("data/runtime/context")
    context_cursor_secret: SecretStr = SecretStr(
        "local-demo-context-cursor-secret-change-in-production"
    )
    deletion_ledger_path: Path = Path("data/runtime/deletions.jsonl")
    skills_root: Path = Path("skills")
    task_token_limit: int = 1_000_000
    daily_token_limit: int = 500_000
    worker_concurrency: int = 4
    lease_seconds: int = 60
    poll_seconds: float = 5.0
    maintenance: bool = False
    fault_drop_receipt: bool = False

    @field_validator(
        "embedding_model_path", "embedding_manifest_path", "embedding_provider", mode="before"
    )
    @classmethod
    def empty_model_path(cls, value):
        return None if value == "" else value

    @model_validator(mode="after")
    def validate_environment(self):
        if self.environment == "production":
            if not self.secure_cookies or not self.app_origin.startswith("https://"):
                raise ValueError("production requires HTTPS and secure cookies")
            if self.fault_drop_receipt:
                raise ValueError("fault injection is forbidden in production")
            if self.context_cursor_secret.get_secret_value().startswith("local-demo"):
                raise ValueError("production requires a unique CONTEXT_CURSOR_SECRET")
        if not self.simulated_business:
            raise ValueError(
                "real business adapter is not configured; simulated_business must remain true"
            )
        if self.model_mode == "qwen" and not self.model_api_key.get_secret_value():
            raise ValueError("MODEL_API_KEY is required for qwen")
        return self
