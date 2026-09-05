"""Tests unitaires des classifieurs (Phase 1).

Couvre : le cache de résultats LRU + TTL, les métriques standardisées,
l'interface ``BaseClassifier`` (ABC), le ``SentimentClassifier`` (avec un
faux prédicteur : aucun torch/transformers n'est chargé) et le registre.

Les faux sont injectés via monkeypatch sur ``_load_predictor`` /
``_reload_predictor`` du module ``sentiment_classifier``.
"""

from __future__ import annotations

import threading
import time

import pytest

from core.classifier_registry import get_registry, reset_registry
from core.prediction_result_cache import PredictionResultCache
from ia.agent.classifiers import sentiment_classifier as sc_module
from ia.agent.classifiers.base import BaseClassifier, ClassifierMetrics, PredictionResult
from ia.agent.classifiers.sentiment_classifier import SentimentClassifier


class FakePredictor:
    """Prédicteur de remplacement : règles lexicales simples."""

    def __init__(self, model_name: str | None = None) -> None:
        self.model_name = model_name
        self.calls = 0

    def predict(self, texts: list[str]) -> list[dict]:
        self.calls += 1
        return [self._one(text) for text in texts]

    def _one(self, text: str) -> dict:
        lowered = text.lower()
        if any(word in lowered for word in ("excellent", "formidable", "adore")):
            label, confidence = "positive", 0.95
        elif "horrible" in lowered or "décev" in lowered or "terrible" in lowered:
            label, confidence = "negative", 0.94
        else:
            label, confidence = "neutral", 0.80
        return {"text": text, "sentiment": label, "confidence": confidence}


class BoomPredictor:
    """Prédicteur qui lève systématiquement (tests de robustesse)."""

    def predict(self, texts: list[str]) -> list[dict]:
        raise RuntimeError("boom")


@pytest.fixture(autouse=True)
def _isole_registre() -> None:
    """Réinitialise le registre singleton avant/après chaque test."""
    reset_registry()
    yield
    reset_registry()


def _use_fake_predictor(monkeypatch: pytest.MonkeyPatch, predictor: object) -> None:
    """Branche un faux prédicteur sur les fonctions module du classifieur."""

    def _load(_model_name: str | None) -> object:
        return predictor

    def _reload(_model_name: str | None) -> object:
        return predictor

    monkeypatch.setattr(sc_module, "_load_predictor", _load)
    monkeypatch.setattr(sc_module, "_reload_predictor", _reload)
# ---------------------------------------------------------------------------
# PredictionResultCache (LRU + TTL)
# ---------------------------------------------------------------------------


class TestPredictionResultCache:
    def test_zero_ttl_never_expires(self) -> None:
        cache = PredictionResultCache(maxsize=8, ttl_seconds=0)
        cache.set("a", 1)
        assert cache.get("a") == 1

    def test_ttl_expiration(self) -> None:
        cache = PredictionResultCache(maxsize=8, ttl_seconds=0.05)
        cache.set("a", "valeur")
        assert cache.get("a") == "valeur"
        time.sleep(0.08)
        assert cache.get("a") is None  # expirée -> miss + éviction
        assert cache.get("a") is None  # clé désormais absente -> miss
        stats = cache.stats()
        assert stats["hits"] == 1
        assert stats["misses"] == 2
        assert stats["evictions"] == 1  # la purge a retiré l'entrée périmée

    def test_miss_counts_and_missing_key(self) -> None:
        cache = PredictionResultCache(maxsize=8, ttl_seconds=0)
        assert cache.get("inconnu") is None
        assert cache.stats()["misses"] == 1
        assert cache.stats()["hit_rate"] == 0.0

    def test_lru_eviction(self) -> None:
        cache = PredictionResultCache(maxsize=2, ttl_seconds=0)
        cache.set("a", 1)
        cache.set("b", 2)
        cache.set("c", 3)
        assert cache.get("a") is None  # évincée (LRU)
        assert cache.get("b") == 2
        assert cache.get("c") == 3
        assert cache.stats()["evictions"] == 1

    def test_lru_refresh_on_access(self) -> None:
        cache = PredictionResultCache(maxsize=2, ttl_seconds=0)
        cache.set("a", 1)
        cache.set("b", 2)
        assert cache.get("a") == 1  # 'a' redevient la plus récente
        cache.set("c", 3)
        assert cache.get("b") is None  # c'est 'b' qui est évincée
        assert cache.get("a") == 1

    def test_make_key_normalization(self) -> None:
        k1 = PredictionResultCache.make_key("sentiment", "  Bonjour ")
        k2 = PredictionResultCache.make_key("sentiment", "bonjour")
        k3 = PredictionResultCache.make_key("sentiment", "bonsoir")
        assert k1 == k2 != k3
        assert len(k1) == 64

    def test_set_replaces_value(self) -> None:
        cache = PredictionResultCache(maxsize=8, ttl_seconds=0)
        cache.set("a", 1)
        cache.set("a", 2)
        assert cache.get("a") == 2
        assert cache.stats()["size"] == 1

    def test_clear_resets_counters(self) -> None:
        cache = PredictionResultCache(maxsize=8, ttl_seconds=0)
        cache.set("a", 1)
        cache.get("a")
        cache.clear()
        assert cache.stats() == {
            "size": 0,
            "maxsize": 8,
            "hits": 0,
            "misses": 0,
            "hit_rate": 0.0,
            "evictions": 0,
            "ttl_seconds": 0,
        }

    def test_maxsize_validation(self) -> None:
        with pytest.raises(ValueError):
            PredictionResultCache(maxsize=0)

    def test_thread_safety(self) -> None:
        cache = PredictionResultCache(maxsize=64, ttl_seconds=0)
        errors: list[Exception] = []

        def work() -> None:
            try:
                for i in range(200):
                    cache.set(f"k{i % 10}", i)
                    cache.get(f"k{i % 10}")
            except Exception as exc:  # pragma: no cover - chemin défensif
                errors.append(exc)

        threads = [threading.Thread(target=work) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        assert cache.stats()["size"] <= 64


# ---------------------------------------------------------------------------
# ClassifierMetrics
# ---------------------------------------------------------------------------


class TestClassifierMetrics:
    def test_record_and_to_dict(self) -> None:
        metrics = ClassifierMetrics()
        metrics.record(5.0, cached=True)
        metrics.record(10.0, cached=False)
        data = metrics.to_dict()
        assert data["predictions"] == 2
        assert data["cached_hits"] == 1
        assert data["cached_misses"] == 1
        assert data["errors"] == 0
        assert data["avg_latency_ms"] == pytest.approx(7.5, abs=1e-3)
        assert data["cache_hit_rate"] == pytest.approx(0.5, abs=1e-4)

    def test_error_recording(self) -> None:
        metrics = ClassifierMetrics()
        metrics.record(0.0, cached=False, error=True)
        assert metrics.to_dict()["errors"] == 1

    def test_empty_state(self) -> None:
        data = ClassifierMetrics().to_dict()
        assert data["predictions"] == 0
        assert data["avg_latency_ms"] == 0.0


# ---------------------------------------------------------------------------
# BaseClassifier (ABC)
# ---------------------------------------------------------------------------


class TestBaseClassifier:
    def test_cannot_instantiate_abstract(self) -> None:
        with pytest.raises(TypeError):
            BaseClassifier()  # type: ignore[abstract]

    def test_default_health_check_is_ok(self) -> None:
        class Dummy(BaseClassifier):
            name = "dummy"

            def load_model(self) -> None:
                pass

            def reload(self) -> None:
                pass

            def predict(self, texts: list[str]) -> list[PredictionResult]:
                return []

        report = Dummy().health_check()
        assert report["ok"] is True

    def test_prediction_result_to_dict(self) -> None:
        result = PredictionResult(text="bonjour", label="positive", confidence=0.9)
        data = result.to_dict()
        assert data["text"] == "bonjour"
        assert "probabilities" not in data  # omis quand None


# ---------------------------------------------------------------------------
# SentimentClassifier
# ---------------------------------------------------------------------------


class TestSentimentClassifier:
    def test_predict_labels_and_order(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = FakePredictor()
        _use_fake_predictor(monkeypatch, fake)
        classifier = SentimentClassifier(model_name="fake-version")

        predictions = classifier.predict(
            ["Ce produit est formidable !", "C'était horrible."]
        )
        assert [p.label for p in predictions] == ["positive", "negative"]
        assert all(not p.cached for p in predictions)
        assert predictions[0].text == "Ce produit est formidable !"
        assert predictions[0].model_name == "fake-version"
        assert predictions[0].confidence == pytest.approx(0.95)

    def test_cache_hit_does_not_retokenize(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = FakePredictor()
        _use_fake_predictor(monkeypatch, fake)
        classifier = SentimentClassifier()

        first = classifier.predict(["Excellent !"])
        second = classifier.predict([" excellent ! "])  # normalisé -> même clé
        assert first[0].label == "positive"
        assert second[0].label == "positive"
        assert second[0].cached is True
        assert fake.calls == 1  # le prédicteur n'a été sollicité qu'une fois

    def test_mixed_batch_misses_batched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = FakePredictor()
        _use_fake_predictor(monkeypatch, fake)
        classifier = SentimentClassifier()
        classifier.predict(["Formidable !"])

        results = classifier.predict(["Formidable !", "Décevant", "Adore ça !"])
        assert [p.cached for p in results] == [True, False, False]
        assert fake.calls == 2  # 1er lot + 1 lot de manqués

    def test_empty_input(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _use_fake_predictor(monkeypatch, FakePredictor())
        classifier = SentimentClassifier()
        assert classifier.predict([]) == []

    def test_metrics_tracking(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _use_fake_predictor(monkeypatch, FakePredictor())
        classifier = SentimentClassifier()
        classifier.predict(["Un", "Deux"])
        classifier.predict(["Un"])  # hit
        metrics = classifier.get_metrics()
        assert metrics["predictions"] == 3
        assert metrics["cached_hits"] == 1
        assert metrics["cached_misses"] == 2
        assert metrics["cache"]["size"] == 2

    def test_predictor_error_increments_and_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _use_fake_predictor(monkeypatch, BoomPredictor())
        classifier = SentimentClassifier()
        with pytest.raises(RuntimeError, match="boom"):
            classifier.predict(["Toujours en échec"])
        assert classifier.get_metrics()["errors"] == 1

    def test_health_check_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _use_fake_predictor(monkeypatch, FakePredictor())
        classifier = SentimentClassifier()
        report = classifier.health_check()
        assert report["ok"] is True
        assert report["label"] in {"positive", "negative", "neutral"}

    def test_health_check_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _use_fake_predictor(monkeypatch, BoomPredictor())
        classifier = SentimentClassifier()
        assert classifier.health_check()["ok"] is False

    def test_get_model_info(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _use_fake_predictor(monkeypatch, FakePredictor())
        classifier = SentimentClassifier(model_name="v-2026")
        info = classifier.get_model_info()
        assert info["name"] == "sentiment"
        assert info["model_name"] == "v-2026"
        assert info["labels"] == ["negative", "neutral", "positive"]

    def test_load_model_force_load(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = FakePredictor()
        _use_fake_predictor(monkeypatch, fake)
        classifier = SentimentClassifier()
        classifier.load_model()
        assert classifier._get_predictor() is fake

    def test_reload_purges_cache(self, monkeypatch: pytest.MonkeyPatch) -> None:
        reloaded = FakePredictor()
        _use_fake_predictor(monkeypatch, reloaded)
        classifier = SentimentClassifier()
        classifier.predict(["Déjà prédit"])  # remplit le cache
        assert classifier.get_metrics()["cache"]["size"] == 1
        classifier.reload()
        assert classifier._get_predictor() is reloaded
        assert classifier.get_metrics()["cache"]["size"] == 0

    def test_probe_warmup_uses_cache(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = FakePredictor()
        _use_fake_predictor(monkeypatch, fake)
        classifier = SentimentClassifier()
        classifier.health_check()
        classifier.health_check()
        assert fake.calls == 1  # la sonde est mise en cache entre les appels


# ---------------------------------------------------------------------------
# ClassifierRegistry
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_register_and_get(self) -> None:
        registry = get_registry()
        classifier = SentimentClassifier()
        registry.register(classifier)
        assert registry.get("sentiment") is classifier
        assert registry.get("inconnu") is None

    def test_register_with_custom_name(self) -> None:
        registry = get_registry()
        classifier = SentimentClassifier()
        registry.register(classifier, name="mon-sentiment")
        assert registry.get("mon-sentiment") is classifier
        assert registry.get("sentiment") is None

    def test_get_or_create_is_idempotent(self) -> None:
        registry = get_registry()
        first = registry.get_or_create("sentiment", lambda: SentimentClassifier())
        second = registry.get_or_create("sentiment", lambda: SentimentClassifier())
        assert first is second

    def test_remove_and_clear(self) -> None:
        registry = get_registry()
        registry.register(SentimentClassifier())
        registry.remove("sentiment")
        assert registry.get("sentiment") is None
        registry.register(SentimentClassifier())
        registry.clear()
        assert registry.names() == []

    def test_names_sorted(self) -> None:
        registry = get_registry()
        registry.register(SentimentClassifier(), name="zeta")
        registry.register(SentimentClassifier(), name="alpha")
        assert registry.names() == ["alpha", "zeta"]

    def test_singleton(self) -> None:
        assert get_registry() is get_registry()
