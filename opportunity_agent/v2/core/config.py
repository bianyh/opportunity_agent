from __future__ import annotations

import os
import secrets
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./data/v2.db")
    redis_url: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    jwt_secret: str = os.getenv("JWT_SECRET") or secrets.token_urlsafe(48)
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = int(os.getenv("ACCESS_TOKEN_MINUTES", "15"))
    refresh_token_days: int = int(os.getenv("REFRESH_TOKEN_DAYS", "14"))
    auto_create_schema: bool = os.getenv("AUTO_CREATE_SCHEMA", "0") == "1"
    embedding_dimension: int = 384
    embedding_model: str = os.getenv("EMBEDDING_MODEL", "intfloat/multilingual-e5-small")
    reranker_model: str = os.getenv("RERANKER_MODEL", "BAAI/bge-reranker-v2-m3")
    reranker_enabled: bool = os.getenv("RERANKER_ENABLED", "0") == "1"
    # Keep the Foundation runnable without the project-specific openJiuwen Rust
    # binding. Deployments opt into real domain services explicitly.
    domain_agent_transport: str = os.getenv("DOMAIN_AGENT_TRANSPORT", "a2a").casefold()
    # The official pure-Python SDK is portable to the Linux Docker image. The
    # Rust binding remains an explicit local-development compatibility option.
    jiuwen_backend: str = os.getenv("JIUWEN_BACKEND", "python").casefold()
    profile_a2a_url: str = os.getenv("PROFILE_A2A_URL", "http://127.0.0.1:8771/a2a/jsonrpc/")
    research_a2a_url: str = os.getenv("RESEARCH_A2A_URL", "http://127.0.0.1:8772/a2a/jsonrpc/")
    planning_a2a_url: str = os.getenv("PLANNING_A2A_URL", "http://127.0.0.1:8773/a2a/jsonrpc/")
    profile_a2a_timeout_seconds: int = int(os.getenv("PROFILE_A2A_TIMEOUT_SECONDS", "90"))
    # Minimum transport timeout; A2A extends Research per request using the
    # dynamic business allowance, including response/loop cleanup overhead.
    research_a2a_timeout_seconds: int = int(os.getenv("RESEARCH_A2A_TIMEOUT_SECONDS", "90"))
    planning_a2a_timeout_seconds: int = int(os.getenv("PLANNING_A2A_TIMEOUT_SECONDS", "120"))


settings = Settings()
