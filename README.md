# Hermes Proxy

An OpenAI-compatible FastAPI proxy running inside Docker that exposes Google LLM models via the Antigravity CLI (`agy`). This allows tools like Hermes or any OpenAI-compatible client running on another machine across the local network to query Google models seamlessly.

---

## Architecture & Features

- **OpenAI-Compatible API**: Implements `POST /v1/chat/completions` (JSON and SSE streaming support) and `GET /v1/models`.
- **Antigravity CLI Integration**: Executes non-interactive `agy` commands and formats responses.
- **Message Flattening**: Parses structured multi-turn conversation arrays (system, user, assistant) into clean prompts.
- **ANSI Code Stripping**: Cleans terminal formatting and ANSI sequences from CLI stdout.
- **OAuth Token Mounting**: Reads the host's existing OAuth credentials (`~/.gemini/antigravity-cli/`) via a read-only bind mount.
- **Always-On Docker Service**: Configured with `restart: unless-stopped` for persistent availability.

---

## File Structure

- [proxy.py](file:///home/agag/Documents/hermes-proxy/proxy.py): FastAPI proxy server handling chat completions, message flattening, ANSI cleaning, and model routing.
- [requirements.txt](file:///home/agag/Documents/hermes-proxy/requirements.txt): Python dependencies (`fastapi`, `uvicorn`, `pydantic`).
- [Dockerfile](file:///home/agag/Documents/hermes-proxy/Dockerfile): Container definition based on `python:3.11-slim` with `agy` CLI installed.
- [docker-compose.yml](file:///home/agag/Documents/hermes-proxy/docker-compose.yml): Service definition with host OAuth volume mount and port mapping (`8000:8000`).

---

## Quickstart

### 1. Start the Container

```bash
docker compose up -d --build
```

### 2. Verify Health Check

```bash
curl http://localhost:8000/health
```

### 3. List Available Models

```bash
curl http://localhost:8000/v1/models
```

### 4. Send a Chat Completion Request

```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.7-flash-high",
    "messages": [
      {"role": "system", "content": "You are a helpful assistant."},
      {"role": "user", "content": "Hello, world!"}
    ]
  }'
```

---

## Connecting from Hermes on LAN

Point Hermes on your remote machine to this server's LAN IP address:

- **Base URL**: `http://<HOST_LAN_IP>:8000/v1`
- **API Key**: Any dummy string (e.g. `sk-hermes-proxy`)
- **Model**: `gemini-3.7-flash-high` (or `gemini-3.7-flash-medium`, `gemini-3.1-pro-high`, `claude-sonnet-4-6`, etc.)
