# Homelab label service (service.py). Needs the host's D-Bus socket for BlueZ.
FROM python:3.13-slim

# Take Debian security fixes newer than the base image, and drop the bundled
# pip: uv installs everything, so pip is only an unused attack surface.
RUN apt-get update \
 && apt-get upgrade -y --no-install-recommends \
 && rm -rf /var/lib/apt/lists/* \
 && pip uninstall -y pip

COPY --from=ghcr.io/astral-sh/uv:0.13 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --locked --no-dev --extra homelab --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --extra homelab

ENV PATH="/app/.venv/bin:$PATH" PORT=8000
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4)"
CMD ["label-service"]
