FROM python:3.12.5-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app PORT=8081
WORKDIR /app
COPY services/indexer/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
    && groupadd --system app \
    && useradd --system --gid app --home-dir /app app
COPY --chown=app:app services /app/services
USER app
EXPOSE 8081
CMD ["python", "services/indexer/main.py"]
