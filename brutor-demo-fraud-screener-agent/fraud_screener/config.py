"""Environment configuration for the fraud screener."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return default if value is None or value == "" else value


CREDENTIALS = ("workload", "api_key")


@dataclass
class Settings:
    gateway_url: str = "http://core:8100"
    # The credential the screener presents (credential.py): "workload", a
    # JWT-SVID from the SPIRE Agent's Workload API at SPIFFE_ENDPOINT_SOCKET; or
    # "api_key", FRAUD_BRUTOR_API_KEY (development stage only).
    credential: str = "workload"
    spiffe_endpoint_socket: str = ""
    workload_audience: str = "brutor"
    api_key: str = ""
    tenant_id: str = "default"
    classifier_model: str = "gpt-5.2"
    public_url: str = "http://brutor-demo-fraud-screener-agent:9200"
    bind_host: str = "0.0.0.0"
    port: int = 9200
    llm_timeout_seconds: float = 20.0
    log_level: str = "INFO"
    # The release's build id, e.g. the git SHA the image was built
    # from; baked in by the Dockerfile's BRUTOR_AGENT_BUILD build arg. Empty =
    # no build header. Name and version come from the installed distribution.
    agent_build: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        credential = _env("BRUTOR_CREDENTIAL", "workload").strip().lower()
        if credential not in CREDENTIALS:
            raise ValueError(f"BRUTOR_CREDENTIAL must be one of {CREDENTIALS}, got {credential!r}")
        return cls(
            gateway_url=_env("BRUTOR_GATEWAY_URL", "http://core:8100").rstrip("/"),
            credential=credential,
            spiffe_endpoint_socket=_env("SPIFFE_ENDPOINT_SOCKET"),
            workload_audience=_env("BRUTOR_WORKLOAD_AUDIENCE", "brutor"),
            # `.demo.env` is shared by both agents and carries the screening
            # agent's key as BRUTOR_API_KEY; the fraud screener's own key is
            # FRAUD_BRUTOR_API_KEY. Prefer the specific name so the container
            # never runs as the wrong identity.
            api_key=_env("FRAUD_BRUTOR_API_KEY") or _env("BRUTOR_API_KEY"),
            tenant_id=_env("BRUTOR_TENANT_ID", "default"),
            classifier_model=_env("CLASSIFIER_MODEL", "gpt-5.2"),
            public_url=_env("A2A_PUBLIC_URL", "http://brutor-demo-fraud-screener-agent:9200").rstrip("/"),
            bind_host=_env("A2A_BIND_HOST", "0.0.0.0"),
            port=int(_env("A2A_PORT", "9200")),
            llm_timeout_seconds=float(_env("LLM_TIMEOUT_SECONDS", "20")),
            log_level=_env("LOG_LEVEL", "INFO").upper(),
            agent_build=_env("BRUTOR_AGENT_BUILD").strip(),
        )
