# Hermes Proxy

An OpenAI-compatible FastAPI proxy running inside Docker that exposes Google LLM models via the Antigravity CLI (`agy`). This allows tools like Hermes or any OpenAI-compatible client running on another machine across the local network to query Google models seamlessly.

---

## Architecture & Features

- **OpenAI-Compatible API**: Implements `POST /v1/chat/completions` (JSON and SSE streaming support) and `GET /v1/models`.
- **Dynamic Latest Flash Model Default**: Automatically discovers available models and defaults to the latest Gemini Flash model at High reasoning level (e.g. `gemini-3.7-flash-high`, and automatically upgrades to `gemini-3.8-flash-high` when available).
- **Virtual Aliases**: Supports `latest`, `gemini-flash`, `gemini-flash-high`, `auto`, and `default` model aliases that resolve dynamically to the newest Flash High model.
- **Antigravity CLI Integration**: Executes non-interactive `agy` commands and formats responses.
- **Message Flattening**: Parses structured multi-turn conversation arrays (system, user, assistant) into clean prompts.
- **ANSI Code Stripping**: Cleans terminal formatting and ANSI sequences from CLI stdout.
- **OAuth Token Mounting**: Reads the host's existing OAuth credentials (`~/.gemini/antigravity-cli/`) via a read-only bind mount.
- **Always-On Docker Service**: Configured with `restart: unless-stopped` for persistent availability.

---

## File Structure

- [proxy.py](file:///home/agag/Documents/hermes-proxy/proxy.py): FastAPI proxy server handling chat completions, dynamic model discovery, message flattening, ANSI cleaning, and model routing.
- [requirements.txt](file:///home/agag/Documents/hermes-proxy/requirements.txt): Python dependencies (`fastapi`, `uvicorn`, `pydantic`).
- [Dockerfile](file:///home/agag/Documents/hermes-proxy/Dockerfile): Container definition based on `python:3.11-slim` with `agy` CLI installed.
- [docker-compose.yml](file:///home/agag/Documents/hermes-proxy/docker-compose.yml): Service definition with host OAuth volume mount, tmpfs overlays, and port mapping (`8000:8000`).

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

Using default dynamic latest Flash High model:
```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "messages": [
      {"role": "system", "content": "You are a helpful assistant."},
      {"role": "user", "content": "Hello, world!"}
    ]
  }'
```

Or explicitly targeting a specific model or `latest` alias:
```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "latest",
    "messages": [
      {"role": "user", "content": "What is quantum computing in one sentence?"}
    ]
  }'
```

---

## Connecting from Hermes on LAN

Point Hermes on your remote machine to this server's LAN IP address:

- **Base URL**: `http://<HOST_LAN_IP>:8000/v1`
- **API Key**: Any dummy string (e.g. `sk-hermes-proxy`)
- **Model**: `latest` (or `gemini-3.7-flash-high`, `gemini-3.1-pro-high`, `claude-sonnet-4-6`, etc.)
