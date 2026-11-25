FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HYDROLOGY_DB=/data/hydrology.db

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY hydrology ./hydrology
COPY scripts ./scripts
COPY samples ./samples
COPY app.py docker-entrypoint.sh ./

RUN chmod +x /app/docker-entrypoint.sh \
    && mkdir -p /data \
    && useradd --uid 10001 --create-home hydrology \
    && chown -R hydrology:hydrology /app /data

USER hydrology
VOLUME ["/data"]
EXPOSE 8000

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["gunicorn", "--workers", "2", "--bind", "0.0.0.0:8000", "--timeout", "30", "app:app"]
