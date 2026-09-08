FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DB_PATH=/data/bot.db

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py db.py embeds.py jellyfin.py scanner.py ./
# Driven by the /imdb slash command, run as a subprocess.
COPY jellyfin_imdb_rename.py ./

# State lives here; docker-compose bind-mounts ./data over it.
VOLUME ["/data"]

CMD ["python", "-u", "bot.py"]
