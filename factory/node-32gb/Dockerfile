FROM python:3.12-slim 
# (Or whatever your base image is)

USER root

# Install the Docker CLI internally
RUN apt-get update && apt-get install -y curl \
    && curl -fsSL https://get.docker.com | sh \
    && apt-get purge -y docker-ce docker-ce-rootless-extras docker-buildx-plugin docker-compose-plugin \
    && rm -rf /var/lib/apt/lists/*

# Verify it exists during the build phase
RUN which docker

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1
