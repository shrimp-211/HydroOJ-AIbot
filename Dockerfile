FROM python:3.12-slim-bookworm

ARG APP_VERSION=2.1.0
LABEL org.opencontainers.image.title="HydroOJ AIbot" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.source="https://github.com/shrimp-211/HydroOJ-AIbot"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    OJ_DATA_DIR=/data

RUN apt-get update \
    && apt-get install -y --no-install-recommends g++ ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 hydro \
    && useradd --uid 10001 --gid hydro --create-home --shell /usr/sbin/nologin hydro \
    && mkdir /data \
    && chown hydro:hydro /data

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
# Root entry scripts stay at /app; implementation and resources live in the package.
COPY *.py ./
# Ship templates only; real config/.env and config/config.json stay on the host or /data.
COPY config/config.example.json config/.env.example config/.env.docker.example ./config/
COPY hydroai/ ./hydroai/

USER 10001:10001
WORKDIR /data
EXPOSE 5000
ENTRYPOINT ["python", "/app/docker_entrypoint.py"]
CMD ["daemon", "--no-interactive"]
