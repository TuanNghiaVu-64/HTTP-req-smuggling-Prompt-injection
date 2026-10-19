FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt 2>/dev/null || true

COPY frontend.py .
COPY backend.py .

# Default command will be overridden in docker-compose.yml
CMD ["python", "-u", "frontend.py"]
