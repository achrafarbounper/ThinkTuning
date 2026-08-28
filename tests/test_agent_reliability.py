# project/tests/test_agent_reliability.py

"""Tests offline de la fiabilité LLM (Phase A, flag AGENT_RELIABILITY).

Couvre ``ia/agent/reliability.py`` — ``classify_llm_error`` (matrice de
classification), ``retry`` (backoff exponentiel + jitter), ``CircuitBreaker``
(machine à états closed -> open -> half_open -> closed) — et l'intégration au
client LLM (retry + breaker actifs quand le flag est posé, inertes sinon).

Aucun réseau : ``requests.post`` est scripté, ``sleep`` / ``monotonic`` sont
pilotés par monkeypatch. Lance avec : pytest tests/test_agent_reliability.py -v
"""

import os
import sys

# Racine « ia/ » dans sys.path (comme core.agent_cache) pour importer l'agent.
_IA_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ia")
if _IA_ROOT not in sys.path:
    sys.path.insert(0, _IA_ROOT)

import pytest  # noqa: E402
import requests  # noqa: E402

from agent import reliability as rel  # noqa: E402
from agent.llm_client import LLMClient  # noqa: E402


# --- Classification des erreurs ------------------------------------------------------


class _Resp:
    """Fausse réponse requests portant un statut (pour HTTPError)."""

    def __init__(self, status):
        self.status_code = status


def test_classify_timeout_retryable():
    ec = rel.classify_llm_error(requests.exceptions.Timeout("boom"))
    assert ec.category == rel.ErrorCategory.TIMEOUT
    assert ec.retryable is True


def test_classify_connection_retryable():
    ec = rel.classify_llm_error(requests.exceptions.ConnectionError("boom"))
    assert ec.category == rel.ErrorCategory.CONNECTION
    assert ec.retryable is True


def test_classify_http_server_retryable():
    ec = rel.classify_llm_error(requests.exceptions.HTTPError(response=_Resp(503)))
    assert ec.category == rel.ErrorCategory.HTTP
    assert ec.retryable is True
    assert ec.http_status == 503


def test_classify_http_rate_limit_retryable():
    ec = rel.classify_llm_error(requests.exceptions.HTTPError(response=_Resp(429)))
    assert ec.retryable is True


def test_classify_http_client_not_retryable():
    for status in (400, 401, 403, 404, 422):
        ec = rel.classify_llm_error(requests.exceptions.HTTPError(response=_Resp(status)))
        assert ec.retryable is False, status


def test_classify_generic_not_retryable():
    ec = rel.classify_llm_error(ValueError("mauvais json"))
    assert ec.retryable is False
    assert ec.category == rel.ErrorCategory.UNKNOWN


def test_classify_circuit_not_retryable():
    ec = rel.classify_llm_error(rel.CircuitBreaker.CallNotPermitted("bloqué"))
    assert ec.category == rel.ErrorCategory.CIRCUIT
    assert ec.retryable is False


# --- Retry à backoff exponentiel ------------------------------------------------------


def test_retry_success_first_attempt():
    calls = []

    def op():
        calls.append(1)
        return "ok"

    assert rel.retry(op, attempts=3, classify=rel.classify_llm_error) == "ok"
    assert len(calls) == 1


def test_retry_recovers_after_transient_errors(monkeypatch):
    calls, sleeps, retried = [], [], []

    def op():
        calls.append(1)
        if len(calls) < 3:
            raise requests.exceptions.ConnectionError("transient")
        return "recovered"

    monkeypatch.setattr(rel.time, "sleep", lambda s: sleeps.append(s))

    result = rel.retry(
        op, attempts=4, base_delay=0.1, max_delay=1.0, jitter=0.0,
        classify=rel.classify_llm_error,
        on_retry=lambda a, e, ec: retried.append((a, ec.category.value)),
    )
    assert result == "recovered"
    assert len(calls) == 3
    assert sleeps == pytest.approx([0.1, 0.2])  # backoff exponentiel (jitter nul)
    assert retried == [(1, "connection"), (2, "connection")]


def test_retry_exhausts_and_raises(monkeypatch):
    monkeypatch.setattr(rel.time, "sleep", lambda _s: None)

    def op():
        raise requests.exceptions.Timeout("x")

    with pytest.raises(requests.exceptions.Timeout):
        rel.retry(op, attempts=3, classify=rel.classify_llm_error)


def test_retry_non_retryable_raises_immediately(monkeypatch):
    monkeypatch.setattr(rel.time, "sleep", lambda _s: None)
    calls = []

    def op():
        calls.append(1)
        raise ValueError("definitif")

    with pytest.raises(ValueError):
        rel.retry(op, attempts=5, classify=rel.classify_llm_error)
    assert len(calls) == 1  # pas de re-tentative sur erreur définitive


def test_retry_without_policy_never_retries(monkeypatch):
    monkeypatch.setattr(rel.time, "sleep", lambda _s: None)
    calls = []

    def op():
        calls.append(1)
        raise requests.exceptions.Timeout("x")

    with pytest.raises(requests.exceptions.Timeout):
        rel.retry(op, attempts=3, classify=None)  # aucune politique = aucun retry
    assert len(calls) == 1
# --- Circuit breaker ---


class _FakeClock:
    """Monotonic pilote : on avance la montre manuellement."""

    def __init__(self, t0=1000.0):
        self.now = t0

    def tick(self, dt):
        self.now += dt

    def __call__(self):
        return self.now


def test_cb_closed_on_success():
    cb = rel.CircuitBreaker(name="t", failures_max=2)
    assert cb.call(lambda: 42) == 42
    assert cb.state == "closed"
    assert cb.failures == 0


def test_cb_opens_after_threshold():
    cb = rel.CircuitBreaker(name="t", failures_max=2)

    def boom():
        raise RuntimeError("echec")

    for _ in range(2):
        with pytest.raises(RuntimeError):
            cb.call(boom)  # l'échec est propagé (le retry décide), et compté
    assert cb.state == "open"
    with pytest.raises(rel.CircuitBreaker.CallNotPermitted):
        cb.call(lambda: "bloque")
    assert cb.failures == 0  # compteur remis a zero a l'ouverture


def test_cb_half_open_probe_success(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(rel.time, "monotonic", clock)
    cb = rel.CircuitBreaker(name="t", failures_max=2, cooldown_seconds=10.0)

    def boom():
        raise RuntimeError("echec")

    for _ in range(2):
        with pytest.raises(RuntimeError):
            cb.call(boom)
    assert cb.state == "open"

    # Avant la fin du cooldown : toujours refuse.
    with pytest.raises(rel.CircuitBreaker.CallNotPermitted):
        cb.call(lambda: "x")
    assert cb.state == "open"

    # Cooldown ecoule -> half_open ; la sonde reussit -> closed.
    clock.tick(10.0)
    assert cb.state == "half_open"
    assert cb.call(lambda: "recupere") == "recupere"
    assert cb.state == "closed"
    assert cb.failures == 0


def test_cb_half_open_probe_failure_reopens(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(rel.time, "monotonic", clock)
    cb = rel.CircuitBreaker(name="t", failures_max=2, cooldown_seconds=5.0)

    def boom():
        raise RuntimeError("echec")

    for _ in range(2):
        with pytest.raises(RuntimeError):
            cb.call(boom)
    clock.tick(5.0)
    assert cb.state == "half_open"
    with pytest.raises(RuntimeError):
        cb.call(boom)  # la sonde echoue -> reopen
    assert cb.state == "open"


def test_cb_reset():
    cb = rel.CircuitBreaker(name="t", failures_max=1)

    def boom():
        raise RuntimeError("echec")

    with pytest.raises(RuntimeError):
        cb.call(boom)
    assert cb.state == "open"
    cb.reset()
    assert cb.state == "closed"
    assert cb.call(lambda: 1) == 1


# --- Integration client LLM (flag AGENT_RELIABILITY) ---


class FakeStreamResp:
    status_code = 200

    def __init__(self, lines):
        self._lines = lines

    def raise_for_status(self):
        pass

    def iter_lines(self, decode_unicode=True):
        yield from self._lines

    def close(self):
        pass


def _client_with_fake_post(monkeypatch, responses):
    """Installe un requests.post scripte et renvoie (client, compteur)."""
    calls = {"n": 0}

    def fake_post(url, **kw):
        calls["n"] += 1
        status = responses.pop(0)
        if isinstance(status, BaseException):
            raise status
        if status != 200:
            resp = FakeStreamResp([])
            resp.status_code = status
            raise requests.exceptions.HTTPError(response=resp)
        return FakeStreamResp(['{"message":{"content":"ok"}}', '{"done":true}'])

    monkeypatch.setattr(requests, "post", fake_post)
    client = LLMClient("http://fake/api/chat", "m:1")
    return client, calls


def test_llm_retry_off_single_call(monkeypatch):
    # Flag off (defaut) : une seule tentative, erreur propagee telle quelle.
    monkeypatch.setenv("AGENT_RELIABILITY", "0")
    responses = [requests.exceptions.ConnectionError("down")]
    client, calls = _client_with_fake_post(monkeypatch, responses)
    with pytest.raises(requests.exceptions.ConnectionError):
        client.call_stream([{"role": "user", "content": "hi"}])
    assert calls["n"] == 1
    assert client.last_error_class is not None
    assert client.last_error_class.retryable is True


def test_llm_retry_recovers_when_flag_on(monkeypatch):
    monkeypatch.setenv("AGENT_RELIABILITY", "1")
    monkeypatch.setattr(rel.time, "sleep", lambda _s: None)
    responses = [
        requests.exceptions.ConnectionError("transient"),
        requests.exceptions.ConnectionError("transient"),
        200,  # succes (une réponse HTTP 200)
    ]
    client, calls = _client_with_fake_post(monkeypatch, responses)
    out = client.call_stream([{"role": "user", "content": "hi"}])
    assert out == "ok"
    assert calls["n"] == 3
    assert client.last_error is None
    assert client.last_error_class is None


def test_llm_http_error_classified(monkeypatch):
    monkeypatch.setenv("AGENT_RELIABILITY", "0")
    responses = [503]
    client, calls = _client_with_fake_post(monkeypatch, responses)
    with pytest.raises(requests.exceptions.HTTPError):
        client.call_stream([{"role": "user", "content": "hi"}])
    assert calls["n"] == 1
    assert client.last_error_class.http_status == 503
    assert client.last_error_class.retryable is True
