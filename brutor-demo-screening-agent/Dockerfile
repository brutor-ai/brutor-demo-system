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
    # A fixed user id: SPIRE's unix workload attestor (spire/README.md) tells the
    # two agents apart by it.
    && groupadd --system --gid 10101 agent \
    && useradd --system --uid 10101 --gid agent --home-dir /app --shell /usr/sbin/nologin agent \
    && mkdir -p /app /data \
    && chown -R agent:agent /app /data

WORKDIR /app
COPY pyproject.toml README.md ./
COPY screening_agent ./screening_agent
RUN pip install --no-cache-dir .

# The release's build id, sent as X-Brutor-Agent-Build next to the
# name and version (those come from pyproject.toml via the installed metadata).
# Pass e.g. the git SHA: docker build --build-arg BRUTOR_AGENT_BUILD=$(git rev-parse HEAD).
# Empty (the default) sends no build header. Declared after the install so a new
# build id does not invalidate the dependency layer.
ARG BRUTOR_AGENT_BUILD=""
ENV BRUTOR_AGENT_BUILD=${BRUTOR_AGENT_BUILD}

USER agent
VOLUME ["/data"]
EXPOSE 9201

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${HEALTH_PORT}/health" || exit 1

CMD ["python", "-m", "screening_agent"]
