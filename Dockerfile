FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DB_PATH=/data/bot.db

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY announce.py bot.py db.py embeds.py httpapi.py jellyfin.py scanner.py version.py ./
# Driven by the /imdb slash command, run as a subprocess.
COPY jellyfin_imdb_rename.py ./

# State lives here; docker-compose bind-mounts ./data over it.
VOLUME ["/data"]

# Reported by /version. Last, so changing them only rebuilds this layer.
# Empty when the image is built without them - /version then falls back to the
# source fingerprint, which is always accurate.
ARG GIT_COMMIT=""
ARG BUILD_DATE=""
ENV GIT_COMMIT=$GIT_COMMIT \
    BUILD_DATE=$BUILD_DATE

CMD ["python", "-u", "bot.py"]
