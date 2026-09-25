"""Small-model API access; no automatic switch to a larger model."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import httpx2

ALLOWED_MODELS = {
    "cloudflare": {"@cf/meta/llama-3.1-8b-instruct-fp8"},
    "openrouter": {"qwen/qwen3-8b", "meta-llama/llama-3.1-8b-instruct"},
}


def assessment_api_config() -> tuple[str, str, str]:
    token = os.getenv("CLOUDFLARE_API_TOKEN", "").strip()
    if token:
        provider = "cloudflare"
        account = os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip()
        if not account or account == "replace_me" or token == "replace_me":
            raise RuntimeError("Configure CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID")
        base = f"https://api.cloudflare.com/client/v4/accounts/{account}/ai/v1"
        model = os.getenv("CLOUDFLARE_MODEL", "@cf/meta/llama-3.1-8b-instruct-fp8").strip()
    else:
        provider = "openrouter"
        token = os.getenv("OPENROUTER_API_KEY", "").strip()
        if not token or token == "replace_me":
            raise RuntimeError("Configure Cloudflare credentials or OPENROUTER_API_KEY")
        base = os.getenv("QWEN_API_BASE_URL", "https://openrouter.ai/api/v1").strip().rstrip("/")
        model = os.getenv("QWEN_MODEL", "qwen/qwen3-8b").strip()
    if not base.startswith("https://"):
        raise ValueError("Model API base URL must use HTTPS")
    if model not in ALLOWED_MODELS[provider]:
        raise ValueError(f"Model {model!r} is not in the verified under-10B allowlist")
    return token, base, model


async def request_json(system: str, context: dict[str, Any]) -> dict[str, Any]:
    token, base, model = assessment_api_config()
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0,
        "max_tokens": 5000,
    }
    async with httpx2.AsyncClient(timeout=httpx2.Timeout(300, connect=30)) as client:
        for attempt in range(2):
            try:
                response = await client.post(
                    f"{base}/chat/completions",
                    headers={"Authorization": f"Bearer {token}"},
                    json=body,
                )
            except (httpx2.TimeoutException, httpx2.NetworkError):
                if attempt:
                    raise
                print("Model connection interrupted; retrying once.", flush=True)
                await asyncio.sleep(2)
                continue
            if response.status_code in {429, 502, 503, 504} and not attempt:
                print(f"Model HTTP {response.status_code}; retrying once.", flush=True)
                await asyncio.sleep(3)
                continue
            response.raise_for_status()
            payload = response.json()
            try:
                choice = payload["choices"][0]
                if choice.get("finish_reason") == "length":
                    raise ValueError("Model response truncated; no output was saved")
                result = json.loads(choice["message"]["content"])
            except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
                raise ValueError("Model did not return a complete JSON object") from exc
            if not isinstance(result, dict):
                raise ValueError("Model result must be a JSON object")
            return result
    raise RuntimeError("Model request failed")
