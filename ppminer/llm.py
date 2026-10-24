"""Client for the Qwen3.8-27B vLLM server (OpenAI-compatible).

The server listens on 127.0.0.1:8060 on the A6000 only. From a laptop, open a
tunnel first:  ssh -N -L 18060:127.0.0.1:8060 qa-a6000
"""
from __future__ import annotations

import json
import os
import re
import time

import requests

LLM_URL = os.getenv("PP_LLM_URL", "http://127.0.0.1:18060/v1")
LLM_MODEL = os.getenv("PP_LLM_MODEL", "Qwen3.8-27B")

_THINK = re.compile(r"<think>.*?</think>", re.S)


class LLMError(RuntimeError):
    pass


def chat(messages: list[dict], *, schema: dict | None = None, think: bool = False,
         max_tokens: int = 1024, temperature: float = 0.3, retries: int = 3, timeout: int = 300) -> str:
    body = {
        "model": LLM_MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "chat_template_kwargs": {"enable_thinking": think},
    }
    if schema:
        body["response_format"] = {"type": "json_schema", "json_schema": {"name": "out", "schema": schema}}
    last = None
    for attempt in range(retries):
        try:
            r = requests.post(f"{LLM_URL}/chat/completions", json=body, timeout=timeout)
            r.raise_for_status()
            text = r.json()["choices"][0]["message"]["content"] or ""
            return _THINK.sub("", text).strip()
        except (requests.RequestException, KeyError, ValueError) as e:
            last = e
            time.sleep(2 * (attempt + 1))
    raise LLMError(f"LLM call failed after {retries} tries: {last}")


def chat_json(messages: list[dict], schema: dict, **kw) -> dict:
    text = chat(messages, schema=schema, **kw)
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise LLMError(f"no JSON in reply: {text[:200]}")
    return json.loads(m.group())
