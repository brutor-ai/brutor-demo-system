# Brutor Demo System: screening agent (LangGraph). See ../DESIGN.md section 5.5.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data \
    HEALTH_PORT=9201

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system agent \
    && useradd --system --gid agent --home-dir /app --shell /usr/sbin/nologin agent \
    && mkdir -p /app /data \
    && chown -R agent:agent /app /data

WORKDIR /app
COPY pyproject.toml README.md ./
COPY screening_agent ./screening_agent
RUN pip install --no-cache-dir .

USER agent
VOLUME ["/data"]
EXPOSE 9201

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${HEALTH_PORT}/health" || exit 1

CMD ["python", "-m", "screening_agent"]
