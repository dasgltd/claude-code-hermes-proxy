#!/usr/bin/env python3
# ==============================================================================
# @file proxy.py
# @version 1.1.0
#
# PROPRIEDADE INTELECTUAL DA DASG CONSULTING LTDA.
# CNPJ 61.628.969/0001-04
# Autor: Daniel A. Silva de la Garza
#
# @description
# claude-proxy: Translates Anthropic Messages API calls to `claude` CLI subprocess calls.
# ==============================================================================
"""
claude-proxy: Translates Anthropic Messages API calls to `claude` CLI subprocess calls.

Third-party apps using a Claude Code OAuth token get HTTP 400
"Third-party apps draw from extra usage" when calling api.anthropic.com directly.
This proxy routes those calls through the official `claude` CLI, which is a
first-party Anthropic app and consumes from the Claude plan limits instead.

Requirements:
  - Claude Code CLI installed and authenticated (`claude` on PATH, or CLAUDE_BIN env var)
  - Python 3.10+, fastapi, uvicorn

Usage:
  python proxy.py                         # listens on 127.0.0.1:11435
  PROXY_PORT=8080 python proxy.py         # custom port
  CLAUDE_BIN=/usr/local/bin/claude python proxy.py

Then point any Anthropic client at the proxy:
  ANTHROPIC_BASE_URL=http://127.0.0.1:11435
  ANTHROPIC_API_KEY=placeholder           # any non-empty value; ignored by proxy

Design notes:
  - The `tools` array from inbound requests is intentionally dropped. Claude Code
    drives its own toolset (file, terminal, etc.); we cannot inject caller-supplied
    tool schemas into a CLI subprocess. Callers that depend on server-side tool_use
    blocks will not get them from this proxy by design.
  - The user prompt is always fed via STDIN (never as an argv positional) so a large
    prompt can never trigger "Argument list too long". The system prompt is written
    to a temp file once it exceeds a safe inline size for the same reason.
"""

import asyncio
import json
import logging
import os
import shutil
import tempfile
import uuid

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

HOST = os.environ.get("PROXY_HOST", "127.0.0.1")
PORT = int(os.environ.get("PROXY_PORT", "11435"))
CLAUDE_BIN = os.environ.get("CLAUDE_BIN") or shutil.which("claude") or "claude"
MAX_TURNS = os.environ.get("PROXY_MAX_TURNS", "30")
# argv stays well under the system limit; anything bigger goes to a temp file.
INLINE_SYSTEM_LIMIT = 4096

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [claude-proxy] %(levelname)s: %(message)s"
)
log = logging.getLogger("claude-proxy")

app = FastAPI(title="claude-proxy")


# ── Helpers ──────────────────────────────────────────────────────────────────


def extract_text(content) -> str:
    """
    Flatten a content value (string or list of Anthropic content blocks) to plain text.

    Anthropic API allows `content` to be either a flat string or a complex array of
    objects (text blocks, tool results). We normalize this into a single string so
    we can hand it to the `claude` binary.

    Args:
        content (str | list): The content payload from an Anthropic message block.

    Returns:
        str: A single flattened string containing all text and tool results.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                parts.append(block.get("text", ""))
            elif btype == "tool_result":
                parts.append(f"[tool result: {extract_text(block.get('content', ''))}]")
        return "\n".join(p for p in parts if p)
    return str(content) if content else ""


def build_prompt_and_system(
    messages: list, system: str | None
) -> tuple[str, str | None]:
    """
    Convert the Anthropic messages array into a single prompt and system prompt.

    The `claude` CLI does not natively accept a JSON array of prior messages in
    stateless mode (`-p`). To simulate memory, we format all historical messages
    into a `<conversation_history>` block and prepend it to the system prompt. The
    last user message becomes the actual CLI prompt (fed via stdin).

    Args:
        messages (list): Array of message objects containing 'role' and 'content'.
        system (str | None): The original system prompt, if any.

    Returns:
        tuple[str, str | None]: (extracted prompt, enriched system prompt).
    """
    if not messages:
        raise HTTPException(status_code=400, detail="messages array is empty")

    last_user_idx = next(
        (i for i in range(len(messages) - 1, -1, -1) if messages[i].get("role") == "user"),
        None,
    )
    if last_user_idx is None:
        raise HTTPException(status_code=400, detail="no user message found")

    prompt = extract_text(messages[last_user_idx].get("content", ""))
    history = messages[:last_user_idx]

    if not history:
        return prompt, system

    lines = []
    for msg in history:
        role = msg.get("role", "user")
        text = extract_text(msg.get("content", ""))
        prefix = "Human" if role == "user" else "Assistant"
        lines.append(f"{prefix}: {text}")

    history_block = (
        "<conversation_history>\n" + "\n\n".join(lines) + "\n</conversation_history>"
    )
    combined = f"{history_block}\n\n{system}" if system else history_block
    return prompt, combined


def build_cmd(
    system: str | None,
    model: str | None,
    system_file: str | None = None,
    stream: bool = False,
) -> list[str]:
    """
    Constructs the CLI command list to execute the claude binary.

    The user prompt is NOT included here — it is always piped via stdin so that an
    arbitrarily large prompt cannot overflow the argv limit. We pass
    `--dangerously-skip-permissions` so the CLI never blocks on human approval, and
    `--no-session-persistence` so API traffic does not pollute local session history.

    Args:
        system (str | None): The system prompt (inline). Used only if system_file is None.
        model (str | None): The Claude model to use.
        system_file (str | None): Path to a temp file holding the system prompt if large.
        stream (bool): Whether to request SSE streaming output from the CLI.

    Returns:
        list[str]: The command array ready for subprocess execution (prompt via stdin).
    """
    cmd = [
        CLAUDE_BIN,
        "-p",
        "--output-format", "stream-json" if stream else "json",
        "--max-turns", MAX_TURNS,
        "--no-session-persistence",
        "--dangerously-skip-permissions",
    ]
    if stream:
        cmd.extend(["--verbose", "--include-partial-messages"])
    if system_file:
        cmd.extend(["--system-prompt-file", system_file])
    elif system:
        cmd.extend(["--system-prompt", system])
    if model and model.startswith("claude-"):
        cmd.extend(["--model", model])
    return cmd


def _maybe_write_tempfile(text: str | None) -> tuple[str | None, str | None]:
    """
    Write text to a temporary file if it exceeds typical shell argument limits.

    The system prompt (enriched with conversation history) can be huge. If it exceeds
    INLINE_SYSTEM_LIMIT characters we dump it to a temp file and tell the CLI to read
    it via --system-prompt-file, keeping argv small.

    Args:
        text (str | None): The text to potentially write.

    Returns:
        tuple[str | None, str | None]: (inline_text, tempfile_path).
        If written to file, inline_text will be None.
    """
    if not text or len(text) <= INLINE_SYSTEM_LIMIT:
        return text, None
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8")
    tmp.write(text)
    tmp.close()
    return None, tmp.name


def _normalize_usage(raw: dict | None) -> dict:
    """
    Map the claude CLI usage object to the Anthropic Messages API usage shape.

    The CLI reports input_tokens, output_tokens, cache_creation_input_tokens and
    cache_read_input_tokens. We surface all of them so the caller (Hermes) can
    budget context accurately instead of seeing zeros.

    Args:
        raw (dict | None): The `usage` object from the CLI result JSON.

    Returns:
        dict: Anthropic-style usage object.
    """
    raw = raw or {}
    return {
        "input_tokens": raw.get("input_tokens", 0) or 0,
        "output_tokens": raw.get("output_tokens", 0) or 0,
        "cache_creation_input_tokens": raw.get("cache_creation_input_tokens", 0) or 0,
        "cache_read_input_tokens": raw.get("cache_read_input_tokens", 0) or 0,
    }


def _estimate_tokens(text: str) -> int:
    """
    Rough token estimate used only by the count_tokens endpoint.

    We have no local tokenizer, so we approximate at ~4 characters per token, which
    is close enough for context-budgeting purposes and far better than a 404.

    Args:
        text (str): Text to estimate.

    Returns:
        int: Estimated token count.
    """
    return max(1, len(text) // 4) if text else 0


# ── Streaming response ────────────────────────────────────────────────────────


async def _drain(stream) -> bytes:
    """Read a subprocess pipe to EOF, returning all bytes (prevents pipe-fill deadlock)."""
    if stream is None:
        return b""
    return await stream.read()


async def _stream_sse(prompt: str, system: str | None, model: str | None):
    """
    Run claude CLI asynchronously and yield Anthropic-compatible SSE events.

    Captures stderr and the process exit code. If the CLI fails (rate limit, auth
    expiry, bad flag) the error text is surfaced INTO the stream instead of silently
    ending with an empty message, so the caller sees the real reason.

    Args:
        prompt (str): The user prompt (sent via stdin).
        system (str | None): The system prompt and history.
        model (str | None): The requested model name.

    Yields:
        str: Formatted SSE payload strings.
    """
    message_id = f"msg_{uuid.uuid4().hex[:24]}"
    actual_model = model or "claude-sonnet-4-6"

    inline_system, system_file = _maybe_write_tempfile(system)
    stderr_task = None
    try:
        cmd = build_cmd(inline_system, model, system_file=system_file, stream=True)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        # Feed the prompt via stdin, then close it so the CLI can proceed.
        proc.stdin.write(prompt.encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()
        # Drain stderr concurrently so a full pipe never deadlocks the process.
        stderr_task = asyncio.create_task(_drain(proc.stderr))

        yield (
            f"event: message_start\n"
            f"data: {json.dumps({'type':'message_start','message':{'id':message_id,'type':'message','role':'assistant','content':[],'model':actual_model,'stop_reason':None,'stop_sequence':None,'usage':{'input_tokens':0,'output_tokens':0}}})}\n\n"
        )
        yield (
            f"event: content_block_start\n"
            f"data: {json.dumps({'type':'content_block_start','index':0,'content_block':{'type':'text','text':''}})}\n\n"
        )
        yield f"event: ping\ndata: {json.dumps({'type':'ping'})}\n\n"

        last_text = ""
        usage = {"input_tokens": 0, "output_tokens": 0}
        saw_error = False
        PING_INTERVAL = 5  # seconds — keeps SSE alive during long tool-use chains

        while True:
            try:
                raw = await asyncio.wait_for(
                    proc.stdout.readline(), timeout=PING_INTERVAL
                )
            except asyncio.TimeoutError:
                yield f"event: ping\ndata: {json.dumps({'type': 'ping'})}\n\n"
                continue

            if not raw:
                break

            line = raw.decode(errors="replace").strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            etype = event.get("type")

            if etype == "assistant":
                msg = event.get("message", {})
                current = "".join(
                    b.get("text", "")
                    for b in msg.get("content", [])
                    if b.get("type") == "text"
                )
                delta = current[len(last_text):]
                if delta:
                    last_text = current
                    yield (
                        f"event: content_block_delta\n"
                        f"data: {json.dumps({'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text':delta}})}\n\n"
                    )
                if msg.get("usage"):
                    usage = _normalize_usage(msg.get("usage"))

            elif etype == "result":
                if event.get("usage"):
                    usage = _normalize_usage(event.get("usage"))
                if event.get("is_error") or event.get("subtype") not in (None, "success"):
                    saw_error = True
                    err = event.get("result") or event.get("error") or "claude CLI reported an error"
                    if not last_text:
                        yield (
                            f"event: content_block_delta\n"
                            f"data: {json.dumps({'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text':f'[claude-proxy error] {err}'}})}\n\n"
                        )

        await proc.wait()
        stderr_bytes = await stderr_task if stderr_task else b""
        stderr_text = stderr_bytes.decode(errors="replace").strip()

        # Process failed and produced nothing useful — surface the real reason.
        if proc.returncode != 0 and not last_text and not saw_error:
            detail = stderr_text or f"claude CLI exited with code {proc.returncode}"
            log.error("streaming subprocess failed: %s", detail)
            yield (
                f"event: content_block_delta\n"
                f"data: {json.dumps({'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text':f'[claude-proxy error] {detail}'}})}\n\n"
            )
        elif proc.returncode != 0 and stderr_text:
            log.warning("streaming subprocess exit %s with stderr: %s", proc.returncode, stderr_text[:500])

        stop_reason = "end_turn" if (proc.returncode == 0 and not saw_error) else "error"

        yield f"event: content_block_stop\ndata: {json.dumps({'type':'content_block_stop','index':0})}\n\n"
        yield (
            f"event: message_delta\n"
            f"data: {json.dumps({'type':'message_delta','delta':{'stop_reason':stop_reason,'stop_sequence':None},'usage':{'output_tokens':usage.get('output_tokens', 0)}})}\n\n"
        )
        yield f"event: message_stop\ndata: {json.dumps({'type':'message_stop'})}\n\n"

    finally:
        if stderr_task and not stderr_task.done():
            stderr_task.cancel()
        if system_file:
            try:
                os.unlink(system_file)
            except OSError:
                pass


# ── Routes ────────────────────────────────────────────────────────────────────


@app.post("/v1/messages")
async def messages_endpoint(request: Request):
    """
    FastAPI endpoint intercepting standard Anthropic API calls.

    Parses the standard JSON body and either streams (SSE) or returns a synchronous
    JSON response. On CLI failure in the non-streaming path it returns a real 502 so
    the caller never sees a silent empty 200.

    Args:
        request (Request): The incoming FastAPI HTTP request.

    Returns:
        StreamingResponse | JSONResponse: The proxied response in Anthropic format.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")

    messages_list = body.get("messages", [])
    system = body.get("system")
    model = body.get("model")
    do_stream = body.get("stream", False)

    if isinstance(system, list):
        system = extract_text(system)

    prompt, combined_system = build_prompt_and_system(messages_list, system)

    if do_stream:
        return StreamingResponse(
            _stream_sse(prompt, combined_system, model),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # Non-streaming
    inline_system, system_file = _maybe_write_tempfile(combined_system)
    try:
        cmd = build_cmd(inline_system, model, system_file=system_file, stream=False)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate(input=prompt.encode("utf-8"))
    finally:
        if system_file:
            try:
                os.unlink(system_file)
            except OSError:
                pass

    stderr_text = (stderr or b"").decode(errors="replace").strip()

    # Hard failure: non-zero exit with no parseable output → real error, not empty 200.
    if proc.returncode != 0:
        detail = stderr_text or f"claude CLI exited with code {proc.returncode}"
        log.error("non-streaming subprocess failed (%s): %s", proc.returncode, detail)
        return JSONResponse(
            status_code=502,
            content={"type": "error", "error": {"type": "api_error", "message": detail}},
        )

    raw_out = stdout.decode(errors="replace")
    text = ""
    usage = {"input_tokens": 0, "output_tokens": 0}
    is_error = False
    try:
        data = json.loads(raw_out)
        text = data.get("result", "")
        usage = _normalize_usage(data.get("usage"))
        is_error = bool(data.get("is_error")) or data.get("subtype") not in (None, "success")
    except (json.JSONDecodeError, AttributeError):
        text = raw_out.strip()

    # CLI ran but reported a logical error, or produced nothing — surface it.
    if is_error or (not text and stderr_text):
        detail = text or stderr_text or "claude CLI reported an error"
        log.error("non-streaming logical error: %s", detail)
        return JSONResponse(
            status_code=502,
            content={"type": "error", "error": {"type": "api_error", "message": detail}},
        )

    return JSONResponse({
        "id": f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "model": model or "claude-sonnet-4-6",
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": usage,
    })


@app.post("/v1/messages/count_tokens")
async def count_tokens_endpoint(request: Request):
    """
    Approximate the Anthropic token-counting endpoint.

    Returns an estimated input_tokens count (~4 chars/token) over the system prompt
    plus all message content. This is an estimate — we have no local tokenizer — but
    it lets the caller budget context instead of hitting a 404 and falling back blind.

    Args:
        request (Request): The incoming FastAPI HTTP request.

    Returns:
        JSONResponse: {"input_tokens": <estimate>}.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")

    system = body.get("system")
    if isinstance(system, list):
        system = extract_text(system)

    total = _estimate_tokens(system or "")
    for msg in body.get("messages", []):
        total += _estimate_tokens(extract_text(msg.get("content", "")))

    return JSONResponse({"input_tokens": total})


@app.get("/health")
async def health():
    """
    Health check endpoint to verify service uptime and CLI detection.

    Returns:
        dict: Status message and the resolved path to the claude binary.
    """
    return {"status": "ok", "claude_bin": CLAUDE_BIN, "max_turns": MAX_TURNS}


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
