from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    llm_provider: Literal["openai_compatible", "gigachat"]
    openai_api_key: str | None
    openai_model: str
    openai_base_url: str | None
    gigachat_credentials: str | None
    gigachat_model: str
    gigachat_scope: str
    openai_timeout_seconds: float
    max_search_calls_per_tool: int
    max_repairs: int


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    provider = os.getenv("LLM_PROVIDER", "openai_compatible").strip().casefold()
    if provider not in {"openai_compatible", "gigachat"}:
        raise ValueError("LLM_PROVIDER must be 'openai_compatible' or 'gigachat'.")

    openai_key = os.getenv("OPENAI_API_KEY", "").strip()
    if openai_key.casefold() in {"", "your-openai-api-key", "replace-me"}:
        openai_key = ""
    gigachat_credentials = os.getenv("GIGACHAT_CREDENTIALS", "").strip()
    if gigachat_credentials.casefold() in {"", "your-gigachat-credentials", "replace-me"}:
        gigachat_credentials = ""

    return Settings(
        llm_provider=provider,
        openai_api_key=openai_key or None,
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        openai_base_url=os.getenv("OPENAI_COMPATIBLE_BASE_URL", "").strip() or None,
        gigachat_credentials=gigachat_credentials or None,
        gigachat_model=os.getenv("GIGACHAT_MODEL", "GigaChat"),
        gigachat_scope=os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS"),
        openai_timeout_seconds=float(os.getenv("OPENAI_TIMEOUT_SECONDS", "30")),
        max_search_calls_per_tool=max(1, min(3, int(os.getenv("AGENT_MAX_SEARCH_CALLS", "3")))),
        max_repairs=max(0, min(1, int(os.getenv("AGENT_MAX_REPAIRS", "1")))),
    )


def prompt_path() -> Path:
    return Path(__file__).with_name("prompts.md")
