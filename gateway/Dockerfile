# Build context is the gateway/ directory (self-contained for `gcloud run deploy --source gateway`).
FROM python:3.12-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY config.yaml .
COPY app app
ENV GATEWAY_CONFIG=/srv/config.yaml
EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
