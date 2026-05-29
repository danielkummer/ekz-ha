FROM python:3.12-slim-bookworm

WORKDIR /app

# Set a valid locale so Chromium doesn't inherit the Pi host's en-US@posix locale,
# which causes RangeError in Date.toLocaleDateString() inside the portal's Vue app.
ENV LANG=en_US.UTF-8 \
    LC_ALL=en_US.UTF-8 \
    LANGUAGE=en_US:en

# Install OS-level deps: Playwright needs these + rsync/ssh for optional remote sync
RUN apt-get update && apt-get install -y \
    ca-certificates \
    fonts-liberation \
    openssh-client \
    rsync \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Chromium and its system dependencies via Playwright
RUN python -m playwright install --with-deps chromium

COPY scraper/ ./scraper/
COPY scripts/ ./scripts/

RUN mkdir -p /app/data/csv /app/data/screenshots /app/data/monthly_snapshots

CMD ["python", "-m", "scraper.main"]

