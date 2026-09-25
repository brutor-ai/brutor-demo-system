FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data \
    MCP_PORT=3014

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . \
    && useradd --system --uid 10001 --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /data \
    && chown -R app:app /data /app

USER app
VOLUME ["/data"]
EXPOSE 3014

# curl is not in python:slim; use the standard library for the health probe.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:3014/health', timeout=4).status == 200 else 1)"

CMD ["python", "-m", "applications_mcp"]
