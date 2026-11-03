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

# [autobook] adds Playwright for auto_submit mode (reCAPTCHA v3 via a real
# headless browser); [autobook-captcha] bakes ddddocr in so the picture-code
# fallback works without host-mounted venvs. Drop both extras to slim the
# image; auto_submit tasks then fall back to plain reminders at captchas.
RUN pip install --no-cache-dir ".[autobook,autobook-captcha]" \
    && python -m playwright install --with-deps chromium

EXPOSE 8000

CMD ["tra-sniper", "serve"]
