"""Environment configuration for the fraud screener."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return default if value is None or value == "" else value


@dataclass
class Settings:
    gateway_url: str = "http://core:8100"
    api_key: str = ""
    tenant_id: str = "default"
    classifier_model: str = "gpt-5.2"
    public_url: str = "http://brutor-demo-fraud-screener-agent:9200"
    bind_host: str = "0.0.0.0"
    port: int = 9200
    llm_timeout_seconds: float = 20.0
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            gateway_url=_env("BRUTOR_GATEWAY_URL", "http://core:8100").rstrip("/"),
            # `.demo.env` is shared by both agents and carries the screening
            # agent's key as BRUTOR_API_KEY; the fraud screener's own key is
            # FRAUD_BRUTOR_API_KEY. Prefer the specific name so the container
            # never runs as the wrong identity (the first live run did exactly
            # that when compose was invoked without the demo.sh env export).
            api_key=_env("FRAUD_BRUTOR_API_KEY") or _env("BRUTOR_API_KEY"),
            tenant_id=_env("BRUTOR_TENANT_ID", "default"),
            classifier_model=_env("CLASSIFIER_MODEL", "gpt-5.2"),
            public_url=_env("A2A_PUBLIC_URL", "http://brutor-demo-fraud-screener-agent:9200").rstrip("/"),
            bind_host=_env("A2A_BIND_HOST", "0.0.0.0"),
            port=int(_env("A2A_PORT", "9200")),
            llm_timeout_seconds=float(_env("LLM_TIMEOUT_SECONDS", "20")),
            log_level=_env("LOG_LEVEL", "INFO").upper(),
        )
