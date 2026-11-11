FROM python:3.10-slim@sha256:6ff506466f8b1e981719b468cc0023b8ef000b983cdec0eafe119d01f379e31d
WORKDIR /app
COPY deploy/embedding.lock /app/embedding.lock
COPY deploy/embedding-wheels /wheels
RUN pip install --no-index --find-links=/wheels --require-hashes -r /app/embedding.lock
COPY deploy/embedding_server.py /app/embedding_server.py
CMD ["uvicorn", "embedding_server:app", "--host", "0.0.0.0", "--port", "8090", "--no-access-log"]
