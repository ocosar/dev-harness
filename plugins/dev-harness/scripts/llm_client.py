#!/usr/bin/env python3
"""Provider-agnostic model call (standard library only) — the harness's pluggable judge.

Used by second_opinion.py, and usable by a repo's own product-eval script as a judge:

  llm_client.py --provider openai|gemini|openrouter|anthropic --model <id> --prompt-file p.md [--image a.jpg ...]
                [--system-file s.md] [--max-tokens 2000] [--key-env NAME] [--json-out usage.json]        # dry run
  … --send                                                                                             # real call

Without --send nothing is sent and no key is read: it prints what WOULD be sent (provider, model, sizes).
`--send` is a PAID call: the harness gate treats any `llm_client.py … --send` command like the repo's other paid
commands (request file + estimate + cap + owner click). The answer text goes to stdout; token usage to --json-out.
The API key is read from the environment variable only, never printed or written anywhere.
"""
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_KEY_ENV = {"openai": "OPENAI_API_KEY", "gemini": "GEMINI_API_KEY", "openrouter": "OPENROUTER_API_KEY",
                   "anthropic": "ANTHROPIC_API_KEY"}
ENDPOINT = {"openai": "https://api.openai.com/v1/chat/completions",
            "openrouter": "https://openrouter.ai/api/v1/chat/completions",
            "anthropic": "https://api.anthropic.com/v1/messages",
            "gemini": "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"}


def _img(path: str) -> tuple[str, str]:
    mime = mimetypes.guess_type(path)[0] or "image/jpeg"
    return mime, base64.b64encode(Path(path).read_bytes()).decode()


def build_request(provider: str, model: str, prompt: str, system: str = "", images: list[str] | None = None,
                  max_tokens: int = 2000, key: str = "") -> tuple[str, dict, dict]:
    """(url, headers, body). Pure — no network; `key` may be empty for a dry run."""
    images = images or []
    if provider in ("openai", "openrouter"):
        content = [{"type": "text", "text": prompt}] + [
            {"type": "image_url", "image_url": {"url": f"data:{m};base64,{b}"}} for m, b in map(_img, images)]
        msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": content}]
        body = {"model": model, "messages": msgs, "max_completion_tokens" if provider == "openai" else "max_tokens": max_tokens}
        return ENDPOINT[provider], {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, body
    if provider == "anthropic":
        content = [{"type": "image", "source": {"type": "base64", "media_type": m, "data": b}}
                   for m, b in map(_img, images)] + [{"type": "text", "text": prompt}]
        body = {"model": model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": content}]}
        if system:
            body["system"] = system
        return ENDPOINT[provider], {"x-api-key": key, "anthropic-version": "2023-06-01",
                                    "Content-Type": "application/json"}, body
    if provider == "gemini":
        parts = [{"text": prompt}] + [{"inline_data": {"mime_type": m, "data": b}} for m, b in map(_img, images)]
        body = {"contents": [{"role": "user", "parts": parts}], "generationConfig": {"maxOutputTokens": max_tokens}}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        return ENDPOINT[provider].format(model=model), {"x-goog-api-key": key, "Content-Type": "application/json"}, body
    raise ValueError(f"unknown provider {provider!r} (openai | gemini | openrouter | anthropic)")


def parse_response(provider: str, data: dict) -> tuple[str, dict]:
    """(text, usage{in, out})."""
    if provider in ("openai", "openrouter"):
        text = "".join((c.get("message") or {}).get("content") or "" for c in data.get("choices", []))
        u = data.get("usage") or {}
        return text, {"in": u.get("prompt_tokens"), "out": u.get("completion_tokens")}
    if provider == "anthropic":
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        u = data.get("usage") or {}
        return text, {"in": u.get("input_tokens"), "out": u.get("output_tokens")}
    text = "".join(p.get("text", "") for c in data.get("candidates", [])
                   for p in (c.get("content") or {}).get("parts", []))
    u = data.get("usageMetadata") or {}
    return text, {"in": u.get("promptTokenCount"), "out": u.get("candidatesTokenCount")}


def estimate_usd(prompt_chars: int, n_images: int, max_out: int, per_mtok_in: float, per_mtok_out: float) -> float:
    """Rough upper estimate: ~4 chars/token, ~1,600 tokens per image, full output budget."""
    tok_in = prompt_chars / 4 + 1600 * n_images
    return round(tok_in / 1e6 * per_mtok_in + max_out / 1e6 * per_mtok_out, 4)


def call(provider: str, model: str, prompt: str, system: str = "", images: list[str] | None = None,
         max_tokens: int = 2000, key_env: str = "", timeout: int = 300) -> tuple[str, dict]:
    key_env = key_env or DEFAULT_KEY_ENV.get(provider, "")
    key = os.environ.get(key_env, "")
    if not key:
        raise RuntimeError(f"environment variable {key_env} is not set (the key is never read from files)")
    url, headers, body = build_request(provider, model, prompt, system, images, max_tokens, key)
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:500].replace(key, "[key]")
        raise RuntimeError(f"{provider} HTTP {e.code}: {detail}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"{provider} unreachable ({e.reason}) — in a cloud sandbox the host may not be on the "
                           "network allowlist") from None
    return parse_response(provider, data)


def main() -> int:
    # allow_abbrev=False: a prefix like `--sen` must NOT be accepted as `--send`, or it would send (spend) while
    # slipping past a paid pattern that matches the full `--send`.
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
                                 allow_abbrev=False)
    ap.add_argument("--provider", required=True, choices=sorted(DEFAULT_KEY_ENV))
    ap.add_argument("--model", required=True)
    ap.add_argument("--prompt-file", required=True)
    ap.add_argument("--system-file")
    ap.add_argument("--image", action="append", default=[])
    ap.add_argument("--max-tokens", type=int, default=2000)
    ap.add_argument("--key-env", default="")
    ap.add_argument("--json-out")
    ap.add_argument("--send", action="store_true", help="really call the API (paid, gated)")
    a = ap.parse_args()
    prompt = Path(a.prompt_file).read_text()
    system = Path(a.system_file).read_text() if a.system_file else ""
    if not a.send:
        url, _, body = build_request(a.provider, a.model, prompt, system, a.image, a.max_tokens)
        print(json.dumps({"dry_run": True, "url": url, "model": a.model, "prompt_chars": len(prompt),
                          "images": len(a.image), "max_tokens": a.max_tokens,
                          "key_env": a.key_env or DEFAULT_KEY_ENV[a.provider],
                          "key_set": bool(os.environ.get(a.key_env or DEFAULT_KEY_ENV[a.provider]))}, indent=2))
        return 0
    try:
        text, usage = call(a.provider, a.model, prompt, system, a.image, a.max_tokens, a.key_env)
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    if a.json_out:
        Path(a.json_out).write_text(json.dumps({"provider": a.provider, "model": a.model, "usage": usage}) + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
