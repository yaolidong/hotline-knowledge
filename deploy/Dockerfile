FROM python:3.10-slim
WORKDIR /app
COPY upstream.lock.json ./
COPY scripts/fetch_upstream.py scripts/fetch_upstream.py
RUN python scripts/fetch_upstream.py
COPY requirements.lock pyproject.toml ./
COPY hotline hotline
RUN pip install --no-cache-dir -r requirements.lock . ./vendor/KAG
ENV HOTLINE_DATA_DIR=/app/data PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["python", "-m", "hotline.cli", "serve", "--host", "0.0.0.0", "--port", "8080"]
