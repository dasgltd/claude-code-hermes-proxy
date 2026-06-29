#!/usr/bin/env python3
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
"""

import asyncio
import json
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

app = FastAPI(title="claude-proxy")


# ── Helpers ──────────────────────────────────────────────────────────────────


def extract_text(content) -> str:
    """Flatten a content value (string or list of Anthropic content blocks) to plain text."""
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
    Convert the Anthropic messages array into (prompt, system_prompt).

    Single user message  → passed directly as the CLI prompt.
    Multi-turn history   → prior turns formatted as <conversation_history> and
                           prepended to the system prompt; last user message
                           becomes the CLI prompt.
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
    prompt: str,
    system: str | None,
    model: str | None,
    system_file: str | None = None,
    stream: bool = False,
) -> list[str]:
    cmd = [
        CLAUDE_BIN,
        "-p", prompt,
        "--output-format", "stream-json" if stream else "json",
        "--max-turns", "1",
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
    """Write text to a temp file if it's long. Returns (system_arg, tempfile_path)."""
    if not text or len(text) <= 4096:
        return text, None
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
    tmp.write(text)
    tmp.close()
    return None, tmp.name


# ── Streaming response ────────────────────────────────────────────────────────


async def _stream_sse(prompt: str, system: str | None, model: str | None):
    """Run claude CLI and yield Anthropic-compatible SSE events."""
    message_id = f"msg_{uuid.uuid4().hex[:24]}"
    actual_model = model or "claude-sonnet-4-6"

    inline_system, system_file = _maybe_write_tempfile(system)
    try:
        cmd = build_cmd(prompt, inline_system, model, system_file=system_file, stream=True)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

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
        output_tokens = 0

        async for raw in proc.stdout:
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
                output_tokens = msg.get("usage", {}).get("output_tokens", output_tokens)

            elif etype == "result":
                output_tokens = event.get("usage", {}).get("output_tokens", output_tokens)

        await proc.wait()

        yield f"event: content_block_stop\ndata: {json.dumps({'type':'content_block_stop','index':0})}\n\n"
        yield (
            f"event: message_delta\n"
            f"data: {json.dumps({'type':'message_delta','delta':{'stop_reason':'end_turn','stop_sequence':None},'usage':{'output_tokens':output_tokens}})}\n\n"
        )
        yield f"event: message_stop\ndata: {json.dumps({'type':'message_stop'})}\n\n"

    finally:
        if system_file:
            try:
                os.unlink(system_file)
            except OSError:
                pass


# ── Routes ────────────────────────────────────────────────────────────────────


@app.post("/v1/messages")
async def messages_endpoint(request: Request):
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
        cmd = build_cmd(prompt, inline_system, model, system_file=system_file, stream=False)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
    finally:
        if system_file:
            try:
                os.unlink(system_file)
            except OSError:
                pass

    text = ""
    try:
        data = json.loads(stdout.decode(errors="replace"))
        text = data.get("result", "")
    except (json.JSONDecodeError, AttributeError):
        text = stdout.decode(errors="replace").strip()

    return JSONResponse({
        "id": f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "model": model or "claude-sonnet-4-6",
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    })


@app.get("/health")
async def health():
    return {"status": "ok", "claude_bin": CLAUDE_BIN}


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
