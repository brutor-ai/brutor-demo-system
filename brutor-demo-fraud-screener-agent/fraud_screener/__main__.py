"""`python -m fraud_screener` serves the agent on A2A_PORT (default 9200)."""

from __future__ import annotations

import logging
import sys

from .app import create_app
from .config import Settings


def main() -> int:
    settings = Settings.from_env()
    logging.basicConfig(level=getattr(logging, settings.log_level, logging.INFO), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    log = logging.getLogger("fraud_screener")
    if not settings.api_key:
        log.warning("BRUTOR_API_KEY is empty: the model step will be skipped and verdicts will say llm_unavailable")
    import uvicorn

    log.info("fraud screener on %s:%d (public %s), gateway %s, model %s", settings.bind_host, settings.port, settings.public_url, settings.gateway_url, settings.classifier_model)
    uvicorn.run(create_app(settings), host=settings.bind_host, port=settings.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
