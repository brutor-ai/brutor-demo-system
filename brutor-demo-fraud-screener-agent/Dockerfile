# Brutor Demo System: Fraud and Sanctions Screener (A2A remote agent). See ../DESIGN.md section 5.4.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    A2A_PORT=9200

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system agent \
    && useradd --system --gid agent --home-dir /app --shell /usr/sbin/nologin agent \
    && mkdir -p /app \
    && chown -R agent:agent /app

WORKDIR /app
COPY pyproject.toml README.md ./
COPY fraud_screener ./fraud_screener
RUN pip install --no-cache-dir .

USER agent
EXPOSE 9200

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${A2A_PORT}/health" || exit 1

CMD ["python", "-m", "fraud_screener"]
