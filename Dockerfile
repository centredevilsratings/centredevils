FROM python:3.12-slim

# System deps for lxml / langdetect
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libxml2-dev \
    libxslt1-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .
COPY x_stream.py .
COPY tweet_drafter.py .
COPY imago.py .
COPY imago_probe.py .
COPY test_webhooks.py .

# Persistent volume mount point for SQLite DB
VOLUME ["/data"]
ENV DB_PATH=/data/football_ops.db

# Run the bot
CMD ["python", "-u", "bot.py"]
