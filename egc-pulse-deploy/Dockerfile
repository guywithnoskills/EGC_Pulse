# EGC Pulse — minimal container for the pure-Python demo backend.
# Stdlib only: no requirements to install. Secrets are NEVER baked in — pass them
# at runtime as environment variables (Render/Railway/Fly secret store, or
# `docker run --env-file demo/.env`). Do not COPY a real .env into the image.
FROM python:3.11-slim

WORKDIR /app
COPY . /app

ENV PORT=8787
EXPOSE 8787

# Persist the SQLite DB to a mounted volume in production: -v pulse-data:/app
CMD ["python3", "pulse_demo.py", "serve"]
