import asyncio
import json
import logging
import os
import re
import shutil
import time
import uuid
from typing import Any, List, Optional, Union

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


# Pydantic Request Models
class ChatMessage(BaseModel):
    role: str
    content: Union[str, List[Any]]
    name: Optional[str] = None


class ChatCompletionRequest(BaseModel):
    model: Optional[str] = Field(default="gemini-3.7-flash-high")
    messages: List[ChatMessage]
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    max_tokens: Optional[int] = None
    stream: Optional[bool] = False


app = FastAPI(
    title="Hermes Antigravity Proxy",
    description="OpenAI-compatible FastAPI proxy to Google LLMs via Antigravity CLI",
    version="1.0.0",
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
    """List supported models in OpenAI format."""
    model_ids = [
        "gemini-3.7-flash-high",
        "gemini-3.7-flash-medium",
        "gemini-3.7-flash-low",
        "gemini-3.6-flash-high",
        "gemini-3.6-flash-medium",
        "gemini-3.6-flash-low",
        "gemini-3.5-flash-high",
        "gemini-3.5-flash-medium",
        "gemini-3.5-flash-low",
        "gemini-3.1-pro-high",
        "gemini-3.1-pro-low",
        "claude-sonnet-4-6",
        "claude-opus-4-6-thinking",
        "gpt-oss-120b-medium",
    ]
    current_time = int(time.time())
    return {
        "object": "list",
        "data": [
            {
                "id": mid,
                "object": "model",
                "created": current_time,
                "owned_by": "google" if "gemini" in mid else "system",
            }
            for mid in model_ids
        ],
    }


async def run_agy_command(prompt: str, model: Optional[str] = None) -> tuple[str, dict]:
    """
    Executes the agy CLI non-interactively with the given prompt.
    Captures stdout, cleans ANSI codes, and extracts response & usage info.
    """
    agy_bin = os.getenv("AGY_BIN") or shutil.which("agy") or "/root/.local/bin/agy"
    target_model = model or os.getenv("DEFAULT_MODEL", "gemini-3.7-flash-high")

    cmd = [
        agy_bin,
        "-p",
        prompt,
        "--disable-slash-commands",
        "--output-format",
        "json",
    ]

    if target_model:
        cmd.extend(["--model", target_model])

    logger.info(f"Executing command for model '{target_model}' (prompt length: {len(prompt)} chars)")

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
    Flattens messages, executes non-interactive agy command, and returns standard JSON.
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

    target_model = req.model or os.getenv("DEFAULT_MODEL", "gemini-3.7-flash-high")
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
