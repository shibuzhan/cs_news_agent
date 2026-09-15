FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
RUN apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=30 update \
    && apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=30 install -y --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY pyproject.toml alembic.ini ./
COPY app ./app
COPY agent_skills ./agent_skills
COPY migrations ./migrations
COPY scripts ./scripts

RUN useradd --create-home --uid 10001 appuser \
    && chmod +x /app/scripts/container-entrypoint.sh
USER appuser

EXPOSE 8000
ENTRYPOINT ["/app/scripts/container-entrypoint.sh"]
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
