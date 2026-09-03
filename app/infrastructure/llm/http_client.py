"""Client HTTP LLM v2 — implémentation propre de ``LLMClientPort`` (httpx).

Remplace progressivement ``ia/agent/llm_client.py`` (Phase 3 de la migration)
derrière le MÊME port, sans changer les use-cases. Reproduit fidèlement le
comportement legacy :

- trois providers : ``ollama`` (NDJSON, ``options.num_ctx`` / ``think``),
  ``openrouter`` et ``hf`` (SSE compatible OpenAI, fragments
  ``choices[0].delta.{content, reasoning}``) ;
- streaming réel ``stream: true`` : chaque fragment émetté via les callbacks
  ``on_thinking`` / ``on_content`` ; ``call()`` réassemble (contrat str
  historique préservé) ;
- fiabilité réutilisée depuis le legacy : ``retry()`` à backoff exponentiel et
  ``CircuitBreaker`` (briques génériques de ``ia/agent/reliability.py``), mais
  avec un classifieur d'erreurs httpx (``errors.py``) ;
- post-traitement aligné v1 : réparation des doubles-encodages UTF-8
  (``repair_utf8_mojibake``) et extraction des balises ``thinking`` inline
  (``extract_thinking``).

Le transport httpx est injectable (``transport=None`` = défaut) : les tests
hors réseau utilisent ``httpx.MockTransport`` sans rien changer au client.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from typing import Any

import httpx

from app.infrastructure.llm.errors import classify_llm_error
from ia.agent.encoding import repair_utf8_mojibake
from ia.agent.reliability import CircuitBreaker, retry
from ia.agent.thinking import extract_thinking

logger = logging.getLogger("thinktuning.agent")
logger.setLevel(os.getenv("AGENT_LOG_LEVEL", "INFO").upper())

PROVIDERS = ("ollama", "openrouter", "hf")
DEFAULT_TEMPERATURE = 0.8
DEFAULT_CONTEXT_LENGTH = 2048


def _parse_chunk(line: Any):
    """Parse une ligne du flux (NDJSON Ollama ou SSE OpenAI).

    Tolère un préfixe « data: », un éventuel ``bytes`` (normalisé en UTF-8), et
    renvoie ``None`` pour toute ligne hors format (vide, ``[DONE]``, JSON
    invalide) sans lever d'exception.
    """
    if isinstance(line, (bytes, bytearray)):
        line = line.decode("utf-8", errors="replace")
    line = line.strip()
    if line.startswith("data:"):
        line = line[len("data:"):].lstrip()
    if not line or line == "[DONE]":
        return None
    try:
        return json.loads(line)
    except (ValueError, TypeError):
        return None


def _build_payload(
    provider: str,
    model: str,
    messages: list[dict],
    temperature: float,
    context_length: int,
    think: bool,
) -> dict:
    """Construit le corps JSON de la requête selon le provider."""
    if provider in ("openrouter", "hf"):
        payload: dict = {
            "model": model,
            "messages": messages,
            "stream": True,
            "temperature": temperature,
        }
    else:
        payload = {
            "model": model,
            "messages": messages,
            "stream": True,
            "options": {
                "num_ctx": context_length,
                "temperature": temperature,
            },
        }
    if think and provider == "ollama":
        payload["think"] = True
    return payload


class HttpLLMClient:
    """Adapter ``LLMClientPort`` — client HTTP multi-provider entièrement streamé.

    Mêmes réglages que le client legacy ; ``transport`` est injectable pour les
    tests (MockTransport), ``retry_*`` / ``circuit_*`` commandent la fiabilité.
    """

    def __init__(
        self,
        url: str,
        model: str,
        provider: str = "ollama",
        api_key: str | None = None,
        timeout: float | None = None,
        temperature: float | None = None,
        context_length: int | None = None,
        think: bool = False,
        transport: httpx.BaseTransport | None = None,
        retry_attempts: int | None = None,
        retry_base_delay: float | None = None,
        circuit_failures: int | None = None,
        circuit_cooldown: float | None = None,
    ) -> None:
        provider = (provider or "ollama").strip().lower()
        if provider not in PROVIDERS:
            raise ValueError(
                f"Provider LLM inconnu : {provider!r} (supportés : {', '.join(PROVIDERS)})"
            )
        self.url = url
        self.model = model
        self.provider = provider
        self.api_key = api_key
        self.timeout = timeout
        self.temperature = DEFAULT_TEMPERATURE if temperature is None else float(temperature)
        self.context_length = (
            DEFAULT_CONTEXT_LENGTH if context_length is None else int(context_length)
        )
        self.think = bool(think)
        self.last_thinking = ""
        self.last_error: BaseException | None = None
        self.last_error_class = None

        self._transport = transport
        self.retry_attempts = (
            retry_attempts
            if retry_attempts is not None
            else int(os.getenv("AGENT_LLM_RETRY_ATTEMPTS", "3"))
        )
        self.retry_base_delay = (
            retry_base_delay
            if retry_base_delay is not None
            else float(os.getenv("AGENT_LLM_RETRY_BASE_DELAY", "0.5"))
        )
        self.circuit_failures = (
            circuit_failures
            if circuit_failures is not None
            else int(os.getenv("AGENT_LLM_CIRCUIT_FAILURES", "5"))
        )
        self.circuit_cooldown = (
            circuit_cooldown
            if circuit_cooldown is not None
            else float(os.getenv("AGENT_LLM_CIRCUIT_COOLDOWN", "30"))
        )
        self._circuit_breaker: CircuitBreaker | None = None
        self._last_client: httpx.Client | None = None

    # --- Interface LLMClientPort --------------------------------------------

    def call(self, messages: list[dict]) -> str:
        """Réponse complète (str) pour un historique de messages."""
        return self.call_stream(messages)

    def call_stream(
        self,
        messages: list[dict],
        on_thinking: Callable[[str], None] | None = None,
        on_content: Callable[[str], None] | None = None,
    ) -> str:
        started = time.perf_counter()
        logger.info(
            "llm_request provider=%s url=%s model=%s messages=%d "
            "timeout=%s streaming=true num_ctx=%d",
            self.provider, self.url, self.model, len(messages),
            self.timeout, self.context_length,
        )
        payload = _build_payload(
            self.provider, self.model, messages,
            self.temperature, self.context_length, self.think,
        )
        try:
            content, thinking = self._stream(payload, on_thinking, on_content)
        except BaseException as exc:  # noqa: BLE001 - re-levée après classification
            self.last_error = exc
            self.last_error_class = classify_llm_error(exc)
            raise

        content = repair_utf8_mojibake(content)
        thinking = repair_utf8_mojibake(thinking.strip())
        # Repli : serveurs anciens laissant les balises thinking inline.
        content, inline_thinking = extract_thinking(content)
        parts = [p for p in (inline_thinking, thinking) if p]
        self.last_thinking = repair_utf8_mojibake("\n\n".join(parts)) if parts else ""

        logger.info(
            "llm_response status=ok elapsed_ms=%.0f content_chars=%d thinking_chars=%d",
            (time.perf_counter() - started) * 1000,
            len(content), len(self.last_thinking),
        )
        logger.debug("llm_response_content=%s", content)
        self.last_error = None
        self.last_error_class = None
        return content

    # --- Fiabilité (retry + circuit breaker) -------------------------------

    def _get_circuit_breaker(self) -> CircuitBreaker:
        if self._circuit_breaker is None:
            self._circuit_breaker = CircuitBreaker(
                name=f"{self.provider}:{self.model}",
                failures_max=self.circuit_failures,
                cooldown_seconds=self.circuit_cooldown,
            )
        return self._circuit_breaker

    def _log_retry(self, attempt: int, exc: BaseException, error_class) -> None:
        ec = error_class.to_dict() if error_class is not None else None
        logger.warning(
            "llm_attempt_failed attempt=%d/%d category=%s error=%s retryable=%s",
            attempt, self.retry_attempts, (ec or {}).get("category"),
            type(exc).__name__, (ec or {}).get("retryable"),
        )

    def _open_stream(self, payload: dict):
        """Ouvre le flux avec retry + circuit breaker (avant le premier octet)."""
        cb = self._get_circuit_breaker()

        def guarded_once():
            return cb.call(lambda: self._open_once(payload))

        return retry(
            guarded_once,
            attempts=self.retry_attempts,
            base_delay=self.retry_base_delay,
            classify=classify_llm_error,
            on_retry=self._log_retry,
        )

    def _headers(self) -> dict[str, str] | None:
        if self.api_key:
            return {"Authorization": f"Bearer {self.api_key}"}
        return None

    def _open_once(self, payload: dict):
        """TENTATIVE UNIQUE : POST + vérification du statut ; flux ouvert."""
        started = time.perf_counter()
        headers = self._headers()
        client = httpx.Client(transport=self._transport, timeout=self.timeout)
        resp = None
        try:
            # httpx : le streaming se fait via `send(request, stream=True)` — le
            # client reste vivant pour consommer le flux ensuite (`iter_lines`).
            request = client.build_request("POST", self.url, json=payload, headers=headers)
            resp = client.send(request, stream=True)
            resp.raise_for_status()
        except (httpx.TimeoutException, httpx.HTTPStatusError, httpx.RequestError):
            logger.error(
                "llm_open_failed url=%s provider=%s elapsed_ms=%.0f",
                self.url, self.provider, (time.perf_counter() - started) * 1000,
            )
            if resp is not None:
                try:
                    resp.close()
                except Exception:  # noqa: BLE001 - nettoyage best-effort
                    pass
            client.close()
            raise
        self._last_client = client
        return resp

    def _stream(self, payload, on_thinking, on_content):
        """Consomme le flux (NDJSON / SSE) et renvoie (content, thinking)."""
        resp = self._open_stream(payload)
        content_parts: list[str] = []
        thinking_parts: list[str] = []
        try:
            for line in resp.iter_lines():
                if not line:
                    continue
                chunk = _parse_chunk(line)
                if chunk is None:
                    continue
                msg = chunk.get("message") or chunk.get("delta") or {}
                delta = msg.get("content") or ""
                think = msg.get("thinking") or ""
                choices = chunk.get("choices") or []
                if choices:
                    oa_delta = choices[0].get("delta") or {}
                    think = (
                        think
                        or oa_delta.get("reasoning")
                        or oa_delta.get("reasoning_content")
                        or ""
                    )
                    delta = delta or oa_delta.get("content") or ""
                if delta:
                    content_parts.append(delta)
                    if on_content is not None:
                        on_content(delta)
                if think:
                    thinking_parts.append(think)
                    if on_thinking is not None:
                        on_thinking(think)
                if chunk.get("done"):
                    break
        finally:
            try:
                resp.close()
            except Exception:  # noqa: BLE001 - déjà fermé
                pass
            if self._last_client is not None:
                self._last_client.close()
                self._last_client = None
        return "".join(content_parts), "".join(thinking_parts)


# Conformité structurelle explicite (signatures vérifiées par test).
_REF: Any = HttpLLMClient
