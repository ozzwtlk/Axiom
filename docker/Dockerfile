# AXIOM server image (role "server"): stores the data and serves the reader. It computes nothing — PCs and laptops
# running `./axiom worker` connect to it and do the work. Multi-arch: builds natively on a Raspberry Pi (arm64).

# ---- reader UI (static files)
FROM node:22-slim AS ui
WORKDIR /ui
RUN npm install -g pnpm@12.17.0
COPY frontend/package.json frontend/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY frontend/ ./
RUN pnpm build

# ---- server
FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY --from=ghcr.io/astral-sh/uv:0.10.11 /uv /usr/local/bin/uv
WORKDIR /app/backend
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY backend/ ./
RUN uv sync --frozen --no-dev
COPY --from=ui /ui/dist /app/frontend/dist
RUN useradd --uid 1000 --create-home axiom && mkdir -p /data && chown axiom:axiom /data
USER axiom
ENV AXIOM_ROLE=server AXIOM_DATA_DIR=/data PATH=/app/backend/.venv/bin:$PATH
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4)"
CMD ["axiom", "serve", "--role", "server", "--host", "0.0.0.0", "--port", "8000"]
