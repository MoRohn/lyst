# syntax=docker/dockerfile:1.7
# Lyst engine (API + workers) and UI share one image; the command picks the role.
FROM python:3.13-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv PATH="/opt/venv/bin:$PATH" LYST_HOME=/var/lib/lyst
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /uvx /bin/
RUN groupadd -r lyst && useradd -r -g lyst -d /var/lib/lyst -m lyst
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --all-extras --no-install-project
COPY src ./src
COPY .streamlit ./.streamlit
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --all-extras
USER lyst
EXPOSE 8770 8501
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD ["python", "-c", \
  "import sys,urllib.request;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8770/health',timeout=3).status==200 else 1)"]
CMD ["lyst", "serve"]
