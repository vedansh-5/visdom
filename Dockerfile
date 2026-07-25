# Build the visdom frontend bundle, then install and run the server.
# Stage 1 — build the web assets (gitignored, produced by webpack).
FROM node:22-slim AS frontend
WORKDIR /src
COPY package.json package-lock.json* ./
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi
COPY . .
RUN npm run build

# Stage 2 — python runtime with the built assets.
FROM python:3.12-slim
WORKDIR /app
COPY --from=frontend /src /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && pip install --no-cache-dir . \
    && rm -rf /var/lib/apt/lists/*

# All runtime config comes from the environment (see the compose .env).
ENV VISDOM_PORT=8097 \
    VISDOM_BASE_URL=/vis \
    VISDOM_ENV_PATH=/data/envs \
    VISDOM_GATEWAY_URL=http://gateway:8085

EXPOSE 8097
CMD ["sh", "-c", "python -m visdom.server -port \"${VISDOM_PORT}\" -base_url \"${VISDOM_BASE_URL}\" -env_path \"${VISDOM_ENV_PATH}\""]
