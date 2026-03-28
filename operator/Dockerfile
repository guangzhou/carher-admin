FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["kopf", "run", "--liveness=http://0.0.0.0:8080/healthz", "-m", "operator.main"]
