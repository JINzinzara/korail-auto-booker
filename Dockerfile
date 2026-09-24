# 로컬 대화형 CLI와 영속 SQLite를 위한 실행 이미지.
FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir . \
    && useradd --create-home booker \
    && mkdir /data && chown booker:booker /data
USER booker
ENV KORAIL_DB_PATH=/data/korail-booker.sqlite3 PYTHONUNBUFFERED=1
ENTRYPOINT ["korail-booker"]
CMD ["run"]
