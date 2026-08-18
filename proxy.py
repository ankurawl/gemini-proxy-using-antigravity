import asyncio
from contextlib import asynccontextmanager
import json
import logging
import os
import re
import shutil
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple, Union

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("hermes-proxy")

# Regex to strip ANSI escape codes
ANSI_ESCAPE_REGEX = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

# In-memory cache for available models
_cached_models: List[Dict[str, Any]] = []
_models_cache_timestamp: float = 0
_cache_lock = asyncio.Lock()
CACHE_TTL_SECONDS = int(os.getenv("MODEL_CACHE_TTL", "1800"))  # 30 minutes


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from text."""
    if not text:
        return ""
    return ANSI_ESCAPE_REGEX.sub("", text)


def flatten_messages(messages: List[Union[dict, Any]]) -> str:
    """
    Flatten an OpenAI-formatted messages list into a structured single text prompt.
    """
    if not messages:
        return ""

    # Single user prompt fast-path
    if len(messages) == 1:
        msg = messages[0]
        role = msg.get("role", "user") if isinstance(msg, dict) else getattr(msg, "role", "user")
        if role == "user":
            content = msg.get("content", "") if isinstance(msg, dict) else getattr(msg, "content", "")
            if isinstance(content, str):
                return content.strip()
            if isinstance(content, list):
                chunks = [
                    part.get("text", "")
                    for part in content
                    if isinstance(part, dict) and part.get("type") == "text"
                ]
                return "\n".join(chunks).strip()

    blocks = []
    for msg in messages:
        if isinstance(msg, dict):
            role = (msg.get("role") or "user").strip().lower()
            content = msg.get("content", "")
        else:
            role = (getattr(msg, "role", "user") or "user").strip().lower()
            content = getattr(msg, "content", "")

        # Extract text from string or structured content parts
        if isinstance(content, list):
            chunks = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    chunks.append(part.get("text", ""))
                elif isinstance(part, str):
                    chunks.append(part)
            text_str = "\n".join(chunks).strip()
        elif isinstance(content, str):
            text_str = content.strip()
        else:
            text_str = str(content or "").strip()

        if not text_str:
            continue

        if role == "system":
            blocks.append(f"[System Instructions]\n{text_str}")
        elif role == "user":
            blocks.append(f"[User]\n{text_str}")
        elif role == "assistant":
            blocks.append(f"[Assistant]\n{text_str}")
        else:
            blocks.append(f"[{role.capitalize()}]\n{text_str}")

    return "\n\n".join(blocks)


async def fetch_available_models(force_refresh: bool = False) -> List[Dict[str, Any]]:
    """
    Fetch available models dynamically from the agy CLI binary.
    Caches the results to minimize latency.
    """
    global _cached_models, _models_cache_timestamp

    now = time.time()
    if not force_refresh and _cached_models and (now - _models_cache_timestamp < CACHE_TTL_SECONDS):
        return _cached_models

    async with _cache_lock:
        if not force_refresh and _cached_models and (now - _models_cache_timestamp < CACHE_TTL_SECONDS):
            return _cached_models

        agy_bin = os.getenv("AGY_BIN") or shutil.which("agy") or "/root/.local/bin/agy"
        models_found: List[Dict[str, Any]] = []

        try:
            proc = await asyncio.create_subprocess_exec(
                agy_bin,
                "models",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=15.0)
            stdout_str = stdout_bytes.decode("utf-8", errors="replace")
            clean_output = strip_ansi(stdout_str)

            current_time = int(time.time())
            for line in clean_output.splitlines():
                line = line.strip()
                if not line or "Fetching available models" in line:
                    continue
                parts = re.split(r"\t+|\s{2,}", line, maxsplit=1)
                if parts:
                    model_id = parts[0].strip()
                    label = parts[1].strip() if len(parts) > 1 else model_id
                    models_found.append({
                        "id": model_id,
                        "label": label,
                        "object": "model",
                        "created": current_time,
                        "owned_by": "google" if "gemini" in model_id.lower() else "system",
                    })
        except Exception as e:
            logger.warning(f"Failed to fetch dynamic models from agy: {e}")

        # If discovery failed, fall back to sensible defaults
        if not models_found:
            default_ids = [
                ("gemini-3.7-flash-medium", "Gemini 3.7 Flash (Medium)"),
                ("gemini-3.7-flash-high", "Gemini 3.7 Flash (High)"),
                ("gemini-3.7-flash-low", "Gemini 3.7 Flash (Low)"),
                ("gemini-3.6-flash-medium", "Gemini 3.6 Flash (Medium)"),
                ("gemini-3.5-flash-medium", "Gemini 3.5 Flash (Medium)"),
                ("gemini-3.1-pro-high", "Gemini 3.1 Pro (High)"),
                ("claude-sonnet-4-6", "Claude Sonnet 4.6 (Thinking)"),
                ("claude-opus-4-6-thinking", "Claude Opus 4.6 (Thinking)"),
                ("gpt-oss-120b-medium", "GPT-OSS 120B (Medium)"),
            ]
            current_time = int(time.time())
            models_found = [
                {
                    "id": mid,
                    "label": label,
                    "object": "model",
                    "created": current_time,
                    "owned_by": "google" if "gemini" in mid else "system",
                }
                for mid, label in default_ids
            ]

        _cached_models = models_found
        _models_cache_timestamp = now
        logger.info(f"Loaded {len(_cached_models)} models from CLI discovery")
        return _cached_models


def find_latest_flash_medium_model(models: List[Dict[str, Any]]) -> str:
    """
    Identifies the latest Gemini Flash model with Medium reasoning level
    by parsing version numbers in descending order (e.g. 3.8 > 3.7 > 3.6).
    """
    candidates: List[Tuple[Tuple[int, ...], str]] = []

    for m in models:
        model_id = m.get("id", "").lower()
        match = re.match(r"^gemini-([\d\.]+)-flash-medium$", model_id)
        if match:
            version_str = match.group(1)
            try:
                ver_tuple = tuple(int(x) for x in version_str.split("."))
                candidates.append((ver_tuple, m["id"]))
            except ValueError:
                continue

    if candidates:
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]

    # Fallback default
    return "gemini-3.7-flash-medium"


def resolve_model_id(requested_model: Optional[str], available_models: List[Dict[str, Any]]) -> str:
    """
    Resolves the target model ID.
    Defaults to the latest Gemini Flash Medium model if unspecified, 'latest',
    or generic fallback names. If a specific recognized model is given, uses it.
    """
    latest_flash_medium = find_latest_flash_medium_model(available_models)

    if not requested_model:
        return latest_flash_medium

    norm = requested_model.strip().lower()

    # Aliases that map directly to the latest flash medium
    generic_aliases = {
        "latest",
        "latest-flash",
        "latest-flash-medium",
        "gemini-flash",
        "gemini-latest",
        "gemini-flash-medium",
        "default",
        "auto",
    }

    if norm in generic_aliases:
        return latest_flash_medium

    # Check if the requested model exists in available models
    known_models = {m["id"].lower(): m["id"] for m in available_models}
    if norm in known_models:
        return known_models[norm]

    # If the caller requested an unknown or client default model (e.g. gpt-4, gpt-4o, hermes),
    # default gracefully to the latest Gemini Flash Medium
    logger.info(f"Requested model '{requested_model}' not directly known; defaulting to '{latest_flash_medium}'")
    return latest_flash_medium


# Pydantic Request Models
class ChatMessage(BaseModel):
    role: str
    content: Union[str, List[Any]]
    name: Optional[str] = None


class ChatCompletionRequest(BaseModel):
    model: Optional[str] = Field(default="latest")
    messages: List[ChatMessage]
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    max_tokens: Optional[int] = None
    stream: Optional[bool] = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Warm up available models cache on startup in background
    asyncio.create_task(fetch_available_models(force_refresh=True))
    yield


app = FastAPI(
    title="Hermes Antigravity Proxy",
    description="OpenAI-compatible FastAPI proxy to Google LLMs via Antigravity CLI",
    version="1.1.0",
    lifespan=lifespan,
)

# Enable CORS for cross-origin or local network access
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "ok", "service": "hermes-proxy"}


@app.get("/v1/models")
async def list_models():
    """List supported models in OpenAI format including dynamic discovery and virtual aliases."""
    available = await fetch_available_models()
    latest_flash_medium = find_latest_flash_medium_model(available)
    current_time = int(time.time())

    # Virtual aliases at the top for convenience
    virtual_aliases = [
        {
            "id": "latest",
            "object": "model",
            "created": current_time,
            "owned_by": "google",
            "description": f"Dynamic alias pointing to {latest_flash_medium}",
        },
        {
            "id": "gemini-flash-medium",
            "object": "model",
            "created": current_time,
            "owned_by": "google",
            "description": f"Dynamic alias pointing to {latest_flash_medium}",
        },
    ]

    all_models = virtual_aliases + [
        {
            "id": m["id"],
            "object": "model",
            "created": m.get("created", current_time),
            "owned_by": m.get("owned_by", "google"),
        }
        for m in available
    ]

    return {"object": "list", "data": all_models}


async def run_agy_command(prompt: str, model: str) -> tuple[str, dict]:
    """
    Executes the agy CLI non-interactively with the given prompt and model.
    Captures stdout, cleans ANSI codes, and extracts response & usage info.
    """
    agy_bin = os.getenv("AGY_BIN") or shutil.which("agy") or "/root/.local/bin/agy"

    cmd = [
        agy_bin,
        "-p",
        prompt,
        "--disable-slash-commands",
        "--output-format",
        "json",
        "--model",
        model,
    ]

    logger.info(f"Executing command with model '{model}' (prompt length: {len(prompt)} chars)")

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=300.0)
    except FileNotFoundError:
        logger.error(f"Antigravity CLI binary not found at '{agy_bin}'")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Antigravity CLI binary '{agy_bin}' not found.",
        )
    except asyncio.TimeoutError:
        logger.error("Antigravity CLI execution timed out after 300s")
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="Antigravity CLI execution timed out.",
        )

    stdout_str = stdout_bytes.decode("utf-8", errors="replace")
    stderr_str = stderr_bytes.decode("utf-8", errors="replace")

    if proc.returncode != 0:
        clean_err = strip_ansi(stderr_str).strip() or strip_ansi(stdout_str).strip()
        logger.error(f"agy exited with code {proc.returncode}: {clean_err}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Antigravity CLI failed (exit {proc.returncode}): {clean_err}",
        )

    response_text = ""
    usage_info = {}

    # Parse JSON output from agy if present
    try:
        data = json.loads(stdout_str)
        if isinstance(data, dict) and "response" in data:
            response_text = data["response"]
            if "usage" in data and isinstance(data["usage"], dict):
                usage_info = data["usage"]
        else:
            response_text = stdout_str
    except Exception:
        response_text = stdout_str

    clean_response = strip_ansi(response_text).strip()
    return clean_response, usage_info


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest):
    """
    OpenAI-compatible chat completion endpoint.
    Flattens messages, resolves model to latest Flash Medium by default, executes agy, and returns standard JSON.
    """
    if not req.messages:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Messages array cannot be empty.",
        )

    prompt = flatten_messages(req.messages)
    if not prompt:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No valid text found in messages.",
        )

    available_models = await fetch_available_models()
    target_model = resolve_model_id(req.model, available_models)

    response_text, raw_usage = await run_agy_command(prompt, model=target_model)

    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created_ts = int(time.time())

    # Build token usage (using reported agy token stats or fallback character estimations)
    prompt_tokens = raw_usage.get("input_tokens", max(1, len(prompt) // 4))
    completion_tokens = raw_usage.get("output_tokens", max(1, len(response_text) // 4))
    total_tokens = raw_usage.get("total_tokens", prompt_tokens + completion_tokens)

    # Handle streaming if requested by client
    if req.stream:
        async def sse_generator():
            # Initial delta role chunk
            role_delta = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created_ts,
                "model": target_model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": ""},
                        "finish_reason": None,
                    }
                ],
            }
            yield f"data: {json.dumps(role_delta)}\n\n"

            # Content delta chunk
            content_delta = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created_ts,
                "model": target_model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": response_text},
                        "finish_reason": None,
                    }
                ],
            }
            yield f"data: {json.dumps(content_delta)}\n\n"

            # Stop delta chunk
            stop_delta = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created_ts,
                "model": target_model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {},
                        "finish_reason": "stop",
                    }
                ],
            }
            yield f"data: {json.dumps(stop_delta)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(sse_generator(), media_type="text/event-stream")

    # Return standard OpenAI JSON chat completion response
    response_payload = {
        "id": completion_id,
        "object": "chat.completion",
        "created": created_ts,
        "model": target_model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": response_text,
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        },
    }

    return JSONResponse(content=response_payload)
