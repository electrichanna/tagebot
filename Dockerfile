FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.11.25 /uv /bin/uv

# ffmpeg converts voice messages for transcription, tzdata makes TZ work for the check-in time
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_NO_DEV=1

# Dependencies first, so code changes don't reinstall them
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-install-project

COPY src ./src

RUN useradd --system --create-home tagebot
USER tagebot

# No volume needed: chat-changed settings are stored on the SMB share
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1

CMD ["python", "src/tagebot/main.py"]
