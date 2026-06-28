FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir fastapi uvicorn[standard] pydantic itsdangerous "psycopg[binary]" psycopg-pool

COPY app/ /app/

RUN mkdir -p /app/data/hourly_snapshots

EXPOSE 9217

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "9217"]