FROM python:3.15-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TGHARBOR_CONFIG=/config/config.yaml

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir .

RUN groupadd --system --gid 10001 tgharbor \
    && useradd --system --uid 10001 --gid tgharbor --home-dir /app tgharbor \
    && mkdir -p /config /data /downloads \
    && chown -R tgharbor:tgharbor /app /data /downloads

VOLUME ["/config", "/data", "/downloads"]

USER tgharbor

ENTRYPOINT ["tgharbor"]
CMD ["run"]
