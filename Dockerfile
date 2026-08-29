FROM python:3.11-slim

ENV PIPELINE_MODE=simulate \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml /app/pyproject.toml
COPY src /app/src
COPY config /app/config
COPY templates /app/templates
COPY fixtures /app/fixtures

RUN pip install --no-cache-dir .

RUN useradd --create-home --uid 10001 pipeline
RUN mkdir -p /output && chown pipeline:pipeline /output

USER pipeline

ENTRYPOINT ["python", "-m", "pipeline"]
