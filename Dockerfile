FROM python:3.13-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/app/data

WORKDIR /app

RUN groupadd --system bot \
    && useradd --system --gid bot --home-dir /app bot

COPY pyproject.toml ./
COPY src ./src

RUN python -m pip install --no-cache-dir . \
    && mkdir -p /app/data \
    && chown bot:bot /app/data

USER bot

FROM runtime AS parser
CMD ["python", "-m", "seminar_bot.parser_service"]

FROM runtime AS bot
VOLUME ["/app/data"]

CMD ["python", "-m", "seminar_bot"]
