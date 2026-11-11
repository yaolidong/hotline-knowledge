from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class User(BaseModel):
    name: str
    role: Literal["admin", "maintainer", "reviewer", "agent"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HOTLINE_", env_file=".env", extra="ignore")
    data_dir: Path = Path("data")
    default_knowledge_space: Literal["production", "test"] = "production"
    users: dict[str, User] = Field(default_factory=dict, repr=False)
    llm_backend: Literal["kag", "openai"] = "kag"
    llm_base_url: str = ""
    llm_model: str = ""
    llm_api_key: SecretStr = SecretStr("")
    llm_timeout: float = Field(90, ge=1, le=600)
    llm_retries: int = Field(2, ge=0, le=5)
    extraction_validation_retries: int = Field(1, ge=0, le=3)
    llm_concurrency: int = Field(2, ge=1, le=8)
    llm_max_tokens: int = Field(12000, ge=256, le=65536)
    llm_extra_body: dict = Field(default_factory=dict)
    qa_context_chars: int = Field(30000, ge=4000, le=100000)
    qa_validation_retries: int = Field(1, ge=0, le=2)
    embedding_base_url: str = ""
    embedding_model: str = ""
    embedding_api_key: SecretStr = SecretStr("")
    embedding_timeout: float = Field(30, ge=1, le=120)
    embedding_max_chunks: int = Field(500, ge=1, le=2000)
    http_trust_env: bool = False
    openspg_url: str = "http://127.0.0.1:8887"
    openspg_project_id: int = 0
    openspg_token: SecretStr = SecretStr("")
    openspg_scalar_encoding: Literal["json", "plain"] = "json"
    namespace: str = Field("Hotline", pattern=r"^[A-Z][A-Za-z0-9]{0,40}$")
    neo4j_uri: str = "bolt://127.0.0.1:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: SecretStr = SecretStr("")
    neo4j_database: str = "neo4j"
    max_upload_mb: int = Field(20, ge=1, le=100)
    max_text_chars: int = Field(500000, ge=100, le=2000000)
    max_chunks: int = Field(300, ge=1, le=1000)
    ocr_enabled: bool = True
    ocr_model_dir: Path | None = None
    ocr_max_pages: int = Field(50, ge=1, le=500)
    ocr_render_side: int = Field(2200, ge=1000, le=3000)
    ocr_threads: int = Field(2, ge=1, le=8)
    ocr_review_threshold: float = Field(0.85, ge=0, le=1)
    worker_enabled: bool = True
    rate_limit_per_minute: float = Field(60, ge=1, le=6000)
    rate_limit_burst: int = Field(20, ge=1, le=500)
    rate_limit_enabled: bool = True

    @model_validator(mode="after")
    def secure_tokens(self):
        if any(len(token) < 24 for token in self.users):
            raise ValueError("用户令牌至少需要 24 个字符")
        return self


class ServiceError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status
