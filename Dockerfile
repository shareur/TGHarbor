FROM python:3.15-slim

ARG TGHARBOR_IMAGE_VERSION=0.2.0
ARG TGHARBOR_IMAGE_REVISION=uncommitted
ARG TGHARBOR_IMAGE_SOURCE=local
ARG TGHARBOR_IMAGE_CREATED=1970-01-01T00:00:00Z

LABEL org.opencontainers.image.title="TGHarbor" \
    org.opencontainers.image.version="${TGHARBOR_IMAGE_VERSION}" \
    org.opencontainers.image.revision="${TGHARBOR_IMAGE_REVISION}" \
    org.opencontainers.image.source="${TGHARBOR_IMAGE_SOURCE}" \
    org.opencontainers.image.licenses="Apache-2.0" \
    org.opencontainers.image.created="${TGHARBOR_IMAGE_CREATED}"

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
