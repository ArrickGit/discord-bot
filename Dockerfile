FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libopus0 curl unzip ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# deno: the JavaScript runtime yt-dlp uses to handle YouTube's player challenges.
RUN arch="$(uname -m)" \
    && case "$arch" in x86_64) t=x86_64-unknown-linux-gnu ;; aarch64) t=aarch64-unknown-linux-gnu ;; esac \
    && curl -fsSL "https://github.com/denoland/deno/releases/latest/download/deno-${t}.zip" -o /tmp/deno.zip \
    && unzip -q /tmp/deno.zip -d /usr/local/bin && rm /tmp/deno.zip

RUN useradd --create-home --uid 1000 musicbot
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot ./bot
COPY config.example.yaml ./

USER musicbot
ENV PYTHONUNBUFFERED=1 DENO_DIR=/app/data/deno-cache
CMD ["python", "-m", "bot"]
