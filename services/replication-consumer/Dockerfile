FROM python:3.12.5-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app PORT=8082
WORKDIR /app
COPY services/replication-consumer/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
    && groupadd --system app \
    && useradd --system --gid app --home-dir /app app
COPY --chown=app:app services /app/services
USER app
EXPOSE 8082
CMD ["python", "services/replication-consumer/reader.py"]
