FROM python:3.12-slim-bookworm

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
COPY *.py prompts.json config.example.json ./
COPY templates/ ./templates/

USER 10001:10001
WORKDIR /data
EXPOSE 5000
ENTRYPOINT ["python", "/app/docker_entrypoint.py"]
CMD ["daemon", "--no-interactive"]
