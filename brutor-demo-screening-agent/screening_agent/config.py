"""Environment configuration for the screening agent (DESIGN.md section 5.5)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


LLM_APIS = ("chat", "responses")


def _llm_api(name: str, default: str) -> str:
    value = _env(name, default).strip().lower()
    if value not in LLM_APIS:
        raise ValueError(f"{name} must be one of {LLM_APIS}, got {value!r}")
    return value


@dataclass
class Settings:
    gateway_url: str = "http://core:8100"
    api_key: str = ""
    tenant_id: str = "default"
    applications_mcp_server_id: str = ""
    bureau_mcp_server_id: str = ""
    skills_mcp_server_id: str = "system-agent-skill-server-default"
    fraud_card_id: str = ""
    fraud_capability: str = "screening.fraud_sanctions"
    classifier_model: str = "gpt-5.2"
    drafter_model: str = "gpt-5.5"
    # Which OpenAI-shaped API each model is called with: "chat"
    # (/v1/proxy/llm/chat/completions) or "responses" (/v1/proxy/llm/responses).
    # gpt-5.5 is Responses-only in the trial catalog, so the drafter defaults
    # to responses. A wrong setting is corrected once per process (gateway.py).
    classifier_api: str = "chat"
    drafter_api: str = "responses"
    tick_seconds: int = 600
    max_per_tick: int = 5
    data_dir: Path = field(default_factory=lambda: Path("/data"))
    health_port: int = 9201
    log_level: str = "INFO"
    skill_name: str = "affordability-check"
    skill_script: str = "affordability.py"
    http_timeout_seconds: float = 120.0
    max_attempts_per_application: int = 3
    # How many times an expired underwriter hold is re-raised before the
    # application is handed off with a note. 0 = unlimited (the queue always
    # holds the current decision until a human acts).
    reraise_max: int = 0
    # How many pending applications a tick lists before excluding held and
    # skipped ones; large so held applications never crowd out the batch.
    list_limit: int = 50

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            gateway_url=_env("BRUTOR_GATEWAY_URL", "http://core:8100").rstrip("/"),
            api_key=_env("BRUTOR_API_KEY"),
            tenant_id=_env("BRUTOR_TENANT_ID", "default"),
            applications_mcp_server_id=_env("APPLICATIONS_MCP_SERVER_ID"),
            bureau_mcp_server_id=_env("BUREAU_MCP_SERVER_ID"),
            skills_mcp_server_id=_env("SKILLS_MCP_SERVER_ID", "system-agent-skill-server-default"),
            fraud_card_id=_env("FRAUD_CARD_ID"),
            fraud_capability=_env("FRAUD_CAPABILITY", "screening.fraud_sanctions"),
            classifier_model=_env("CLASSIFIER_MODEL", "gpt-5.2"),
            drafter_model=_env("DRAFTER_MODEL", "gpt-5.5"),
            classifier_api=_llm_api("CLASSIFIER_API", "chat"),
            drafter_api=_llm_api("DRAFTER_API", "responses"),
            tick_seconds=_env_int("TICK_SECONDS", 600),
            max_per_tick=_env_int("MAX_PER_TICK", 5),
            reraise_max=_env_int("RERAISE_MAX", 0),
            list_limit=_env_int("LIST_LIMIT", 50),
            data_dir=Path(_env("DATA_DIR", "/data")),
            health_port=_env_int("HEALTH_PORT", 9201),
            log_level=_env("LOG_LEVEL", "INFO").upper(),
        )

    def missing(self) -> list[str]:
        """Names of required settings that are empty."""
        required = {
            "BRUTOR_API_KEY": self.api_key,
            "APPLICATIONS_MCP_SERVER_ID": self.applications_mcp_server_id,
            "BUREAU_MCP_SERVER_ID": self.bureau_mcp_server_id,
            "FRAUD_CARD_ID": self.fraud_card_id,
        }
        return [name for name, value in required.items() if not value]
