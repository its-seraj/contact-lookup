FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# Hugging Face runs Docker Spaces as uid 1000.
RUN useradd -m -u 1000 user
WORKDIR /app

COPY requirements-api.txt .
RUN pip install -r requirements-api.txt

COPY --chown=user . .
RUN mkdir -p /app/out && chown -R user:user /app

USER user
EXPOSE 7860

# API only: /api/scan, /api/domains, /api/health (+ /docs). No UI is served from here.
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "7860", "--proxy-headers", "--forwarded-allow-ips", "*"]
