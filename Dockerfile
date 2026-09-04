FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-chi-tra \
        tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md LICENSE ./
COPY src ./src

# The browser extra installs the playwright Python package so the API can drive
# the sidecar over CDP. `playwright install` is deliberately NOT run here: the
# browser binaries live in the sidecar image, which keeps this image small.
RUN pip install --no-cache-dir ".[browser]"

EXPOSE 8000

CMD ["tra-sniper", "serve"]
