FROM python:3.10-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends git ca-certificates curl && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY src /app/src
COPY config.example.yml /app/config.example.yml
COPY README.md /app/README.md

ENV CONFIG_PATH=/app/config.yml
ENV PYTHONPATH=/app/src

EXPOSE 8080

CMD ["uvicorn", "codeberg_bridge.app:app", "--host", "0.0.0.0", "--port", "8080"]
