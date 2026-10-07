# Reproducible, no-Python-needed way to run aptfinder.
#   docker build -t aptfinder .
#   docker run -p 8787:8787 -v aptfinder-data:/data aptfinder
# then open http://localhost:8787
FROM python:3.11-slim

WORKDIR /app
COPY . .
RUN pip install --no-cache-dir .

# app state (SQLite DB, preferences, cache) persists in this volume
ENV HOME=/data
VOLUME /data
EXPOSE 8787

# bind to all interfaces so the port is reachable from the host; no browser in a container
CMD ["aptfinder", "web", "--host", "0.0.0.0", "--port", "8787", "--no-browser"]
