#!/usr/bin/env python3
"""Judge runner for Gemini: prompt on stdin, claude-json on stdout.

Reads GEMINI_API_KEY from the environment. Prints only the reply; failures go
to stderr as a short message that never includes the key or request headers.
"""

import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Optional


MODEL = "gemini-3.8-flash"
URL = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
RETRY_WINDOW_SECONDS = 120
MAX_BACKOFF_SECONDS = 30
REQUEST_TIMEOUT_SECONDS = 120


def _retry_delay(retry_after: Optional[str], attempt: int) -> float:
    """Honor a positive numeric Retry-After, otherwise back off exponentially."""
    try:
        delay = float(retry_after or 0)
    except ValueError:  # Retry-After may also be an HTTP date.
        delay = 0
    return delay if delay > 0 else min(2**attempt, MAX_BACKOFF_SECONDS)


def _post(body: bytes, key: str) -> bytes:
    """POST the request, waiting out 429s inside the retry window.

    judge.py only backs off for about three seconds before skipping a group,
    which is too short for a per-minute free-tier quota to recover.
    """
    request = urllib.request.Request(
        URL,
        data=body,
        headers={"x-goog-api-key": key, "Content-Type": "application/json"},
        method="POST",
    )
    deadline = time.monotonic() + RETRY_WINDOW_SECONDS
    attempt = 0
    while True:
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code != 429:
                raise
            delay = _retry_delay(exc.headers.get("Retry-After"), attempt)
            if time.monotonic() + delay > deadline:
                raise
            time.sleep(delay)
            attempt += 1


def _to_claude_json(raw: bytes) -> dict[str, Any]:
    """Translate a generateContent reply; raises on any shape it cannot read."""
    payload = json.loads(raw)
    parts = payload["candidates"][0]["content"]["parts"]
    text = "".join(part["text"] for part in parts if "text" in part and not part.get("thought"))
    if not text.strip():
        raise ValueError("no text")
    usage = payload.get("usageMetadata") or {}
    output_tokens = usage.get("candidatesTokenCount")
    if output_tokens is not None:
        output_tokens += usage.get("thoughtsTokenCount") or 0  # Thinking bills as output.
    return {
        "result": text,
        "usage": {"input_tokens": usage.get("promptTokenCount"), "output_tokens": output_tokens},
        "total_cost_usd": None,  # The API reports no cost; the harness reads null as unknown.
    }


def _fail(message: str) -> int:
    print(f"gemini_judge: {message}", file=sys.stderr)
    return 1


def main() -> int:
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    # http.client quotes a rejected header value in its error, so refuse it here.
    if not key or not (key.isascii() and key.isprintable()):
        return _fail("GEMINI_API_KEY is missing or not a valid header value")
    body = json.dumps(
        {
            "contents": [{"parts": [{"text": sys.stdin.read()}]}],
            "generationConfig": {"thinkingConfig": {"thinkingLevel": "low"}},
        }
    ).encode("utf-8")
    try:
        raw = _post(body, key)
    except urllib.error.HTTPError as exc:
        return _fail(f"Gemini API returned HTTP {exc.code}")
    except (OSError, http.client.HTTPException) as exc:
        return _fail(f"request failed: {type(exc).__name__}")
    try:
        reply = _to_claude_json(raw)
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        return _fail("Gemini API returned no usable text")
    print(json.dumps(reply))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
