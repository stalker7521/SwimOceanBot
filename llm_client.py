"""Chat completions through any OpenAI-compatible provider configured via LLM_* env vars."""

import logging
import re
import time

import requests

from settings import LLM_MODELS, LLM_TOKEN, LLM_URL

CONNECT_TIMEOUT = 30
READ_TIMEOUT = 300
ATTEMPTS = 3
PAUSES_BETWEEN_ATTEMPTS = (30, 90)

THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL)


class LLMError(Exception):
    """All attempts with all models failed."""


def _strip_reasoning(content: str) -> str:
    content = THINK_BLOCK.sub("", content)
    # Если модель оборвала рассуждение без открывающего тега, оставляем то, что после закрывающего
    if "</think>" in content:
        content = content.split("</think>", 1)[1]
    return content.strip()


def _request(model: str, messages: list[dict], temperature: float) -> str:
    response = requests.post(
        LLM_URL,
        headers={"Authorization": f"Bearer {LLM_TOKEN}"},
        json={"model": model, "messages": messages, "temperature": temperature},
        timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
    )
    if response.status_code != 200:
        raise LLMError(f"HTTP {response.status_code}: {response.text[:300]}")
    content = response.json()["choices"][0]["message"].get("content") or ""
    content = _strip_reasoning(content)
    if not content:
        raise LLMError("пустой ответ")
    return content


def complete(messages: list[dict], temperature: float = 0.8, attempts: int = ATTEMPTS) -> str:
    """Up to `attempts` rounds in a row; each round tries every model from LLM_MODELS."""
    if not (LLM_TOKEN and LLM_URL and LLM_MODELS):
        raise LLMError("не заданы LLM_TOKEN, LLM_URL или LLM_MODELS")

    last_error = None
    for attempt in range(attempts):
        for model in LLM_MODELS:
            started = time.monotonic()
            try:
                content = _request(model, messages, temperature)
                logging.info(f"LLM {model} ответила за {time.monotonic() - started:.0f} с")
                return content
            except Exception as e:
                last_error = e
                logging.warning(f"LLM {model}, попытка {attempt + 1}/{attempts} не удалась: {e}")
        if attempt < attempts - 1:
            time.sleep(PAUSES_BETWEEN_ATTEMPTS[min(attempt, len(PAUSES_BETWEEN_ATTEMPTS) - 1)])
    raise LLMError(f"LLM недоступна после {attempts} попыток: {last_error}")
