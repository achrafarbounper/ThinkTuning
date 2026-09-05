"""Tests Phase 2 : DynamicBatcher, InferenceExecutor, ModelWarmup, route async.

Aucune dépendance lourde (torch/transformers) : l'inférence est simulée par des
faux prédicteurs thread-safe. Les threads des batchers/executors/warmup sont
systématiquement arrêtés en fin de test (fixture ``_nettoyage_infra``).
"""

from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from core.dynamic_batcher import DynamicBatcher
from core.inference_executor import InferenceExecutor
from core.model_warmup import ModelWarmup
from ia.agent.classifiers.base import BaseClassifier, PredictionResult


class _FakeInference:
    """Inférence batch simulée : retourne ``r:<texte>`` pour chaque texte."""

    def __init__(self) -> None:
        self.batch_sizes: list[int] = []
        self.calls = 0
        self._lock = threading.Lock()

    def __call__(self, texts: list[str]) -> list[str]:
        with self._lock:
            self.calls += 1
            self.batch_sizes.append(len(texts))
        time.sleep(0.005)  # simule un coût d'inférence
        return [f"r:{t}" for t in texts]


class _BoomInference:
    """Inférence qui lève systématiquement."""

    def __call__(self, texts: list[str]) -> list[str]:
        raise RuntimeError("boom-inférence")


class _TrackingClassifier(BaseClassifier):
    """Classifieur de test pour le warmup (charges + sondes enregistrées)."""

    name = "test-tracking"

    def __init__(self, load_delay: float = 0.0, fail_load: bool = False) -> None:
        self.load_delay = load_delay
        self.fail_load = fail_load
        self.load_calls = 0
        self.health_calls = 0

    def load_model(self) -> None:
        self.load_calls += 1
        if self.load_delay:
            time.sleep(self.load_delay)
        if self.fail_load:
            raise RuntimeError("modèle indisponible")

    def reload(self) -> None:
        self.load_model()

    def predict(self, texts: list[str]) -> list[PredictionResult]:
        return [PredictionResult(text=t, label="neutral", confidence=0.5) for t in texts]

    def health_check(self) -> dict:
        self.health_calls += 1
        return {"ok": True, "label": "neutral", "accuracy_sim": 0.9}


@pytest.fixture
def _nettoyage_infra():
    """Nettoie les singletons globaux (executor/warmup) après chaque test."""
# ---------------------------------------------------------------------------
# DynamicBatcher
# ---------------------------------------------------------------------------


class TestDynamicBatcher:
    def test_regroupe_les_soumissions_une_fenetre(self) -> None:
        fake = _FakeInference()
        with DynamicBatcher(fake, max_batch_size=8, window_seconds=0.05) as batcher:
            pool = ThreadPoolExecutor(max_workers=3)
            try:
                futures = [pool.submit(batcher.submit, f"t{i}") for i in range(3)]
                results = [f.result() for f in futures]
            finally:
                pool.shutdown(wait=True)
        assert sorted(results) == sorted(f"r:t{i}" for i in range(3))
        assert fake.calls == 1  # tout le lot groupé en UNE inférence
        assert sum(fake.batch_sizes) == 3

    def test_max_batch_decoupe_les_lots(self) -> None:
        fake = _FakeInference()
        with DynamicBatcher(fake, max_batch_size=2, window_seconds=0.05) as batcher:
            pool = ThreadPoolExecutor(max_workers=2)
            try:
                futures_a = [pool.submit(batcher.submit, f"a{i}") for i in range(2)]
                results_a = [f.result() for f in futures_a]
                futures_b = [pool.submit(batcher.submit, "seul") for _ in range(1)]
                results_b = [f.result() for f in futures_b]
            finally:
                pool.shutdown(wait=True)
        assert results_a == ["r:a0", "r:a1"]
        assert results_b == ["r:seul"]
        assert fake.calls >= 2  # 1 lot de 2 + 1 lot isolé
        assert all(size <= 2 for size in fake.batch_sizes)

    def test_ordre_des_resultats_preserve(self) -> None:
        fake = _FakeInference()
        with DynamicBatcher(fake, max_batch_size=4, window_seconds=0.02) as batcher:
            pool = ThreadPoolExecutor(max_workers=4)
            try:
                futures = [pool.submit(batcher.submit, f"t{i}") for i in range(4)]
            finally:
                pool.shutdown(wait=True)
            for i, f in enumerate(futures):
                assert f.result() == f"r:t{i}"

    def test_erreur_propagee_a_tous_les_demandeurs(self) -> None:
        with DynamicBatcher(_BoomInference(), max_batch_size=4, window_seconds=0.02) as batcher:
            pool = ThreadPoolExecutor(max_workers=2)
            try:
                futures = [pool.submit(batcher.submit, "x") for _ in range(2)]
                errors = []
                for f in futures:
                    try:
                        f.result()
                    except RuntimeError as exc:
                        errors.append(exc)
            finally:
                pool.shutdown(wait=True)
        assert len(errors) == 2
        assert all(str(e) == "boom-inférence" for e in errors)

    def test_stop_livre_les_demandeurs_en_attente(self) -> None:
        batcher = DynamicBatcher(_FakeInference(), max_batch_size=4, window_seconds=0.05)
        batcher.stop(wait=True, timeout=2.0)
        with pytest.raises(RuntimeError, match="arrêté"):
            batcher.submit("après stop")

    def test_validation_des_parametres(self) -> None:
        with pytest.raises(ValueError):
            DynamicBatcher(_FakeInference(), max_batch_size=0)
        with pytest.raises(ValueError):
            DynamicBatcher(_FakeInference(), window_seconds=-1)
        with pytest.raises(ValueError):
            DynamicBatcher(_FakeInference(), max_queue=0)

    def test_stats_coherentes(self) -> None:
        fake = _FakeInference()
        with DynamicBatcher(fake, max_batch_size=8, window_seconds=0.02) as batcher:
            pool = ThreadPoolExecutor(max_workers=2)
            try:
                futures = [pool.submit(batcher.submit, "t") for _ in range(2)]
                [f.result() for f in futures]
            finally:
                pool.shutdown(wait=True)
            stats = batcher.stats()
        assert stats["processed"] == 2
        assert stats["avg_batch_size"] == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# InferenceExecutor
# ---------------------------------------------------------------------------


class TestInferenceExecutor:
    def test_run_sync(self, _nettoyage_infra) -> None:
        executor = InferenceExecutor(max_workers=2)
        try:
            assert executor.run(lambda: 42) == 42
        finally:
            executor.shutdown()

    def test_submit_retourne_un_future(self, _nettoyage_infra) -> None:
        executor = InferenceExecutor(max_workers=2)
        try:
            future = executor.submit(lambda x: x * 2, 21)
            assert future.result() == 42
        finally:
            executor.shutdown()

    def test_run_async(self, _nettoyage_infra) -> None:
        executor = InferenceExecutor(max_workers=2)
        try:
            result = asyncio.run(executor.run_async(lambda a, b: a + b, 20, 22))
            assert result == 42
        finally:
            executor.shutdown()

    def test_run_async_avec_kwargs(self, _nettoyage_infra) -> None:
        executor = InferenceExecutor(max_workers=2)
        try:
            result = asyncio.run(executor.run_async(lambda **kw: kw["b"] - kw["a"], a=10, b=52))
            assert result == 42
        finally:
            executor.shutdown()

    def test_stats(self, _nettoyage_infra) -> None:
        executor = InferenceExecutor(max_workers=2)
        try:
            executor.submit(lambda: None)
            stats = executor.stats()
            assert stats["max_workers"] == 2
            assert stats["submitted"] >= 1
        finally:
            executor.shutdown()

    def test_exception_propagee(self, _nettoyage_infra) -> None:
        executor = InferenceExecutor(max_workers=2)
        try:

            def _boom() -> None:
                raise ValueError("au milieu du lot")

            with pytest.raises(ValueError, match="au milieu du lot"):
                executor.run(_boom)
        finally:
            executor.shutdown()


# ---------------------------------------------------------------------------
# ModelWarmup
# ---------------------------------------------------------------------------


class TestModelWarmup:
    def test_warm_synchrone(self) -> None:
        warmup = ModelWarmup()
        classifier = _TrackingClassifier()
        report = warmup.warm(classifier)
        assert report["ok"] is True
        assert report["label"] == "neutral"
        assert classifier.load_calls == 1
        assert classifier.health_calls == 1
        assert warmup.is_warmed(classifier.name) is True
        assert warmup.status(classifier.name)["ok"] is True

    def test_warm_background_fin_par_success(self) -> None:
        warmup = ModelWarmup()
        classifier = _TrackingClassifier(load_delay=0.02)
        thread = warmup.warm_in_background(classifier)
        assert thread.is_alive() is True  # tourne en arrière-plan
        thread.join(timeout=5.0)
        assert warmup.is_warmed(classifier.name) is True
        assert classifier.health_calls == 1

    def test_warm_background_idempotent(self) -> None:
        warmup = ModelWarmup()
        classifier = _TrackingClassifier(load_delay=0.1)
        first = warmup.warm_in_background(classifier)
        second = warmup.warm_in_background(classifier)
        assert second is first  # déjà en cours : on réutilise le thread
        first.join(timeout=5.0)
        assert warmup.is_warmed(classifier.name) is True

    def test_echec_ne_leve_jamais(self) -> None:
        warmup = ModelWarmup()
        classifier = _TrackingClassifier(fail_load=True)
        report = warmup.warm(classifier)
        assert report["ok"] is False
        assert "modèle indisponible" in report["error"]
        assert warmup.is_warmed(classifier.name) is False

    def test_snapshot_expose_l_etat(self) -> None:
        warmup = ModelWarmup()
        warmup.warm(_TrackingClassifier())
        snapshot = warmup.snapshot()
        assert snapshot["test-tracking"]["ok"] is True
# ---------------------------------------------------------------------------
# Route asynchrone /predict/batched (via TestClient)
# ---------------------------------------------------------------------------


class _RouteFakePredictor:
    """Prédicteur de la route : réponses statiques (aucun modèle chargé)."""

    def predict(self, texts: list[str]) -> list[dict]:
        return [
            {"text": text, "sentiment": "positive", "confidence": 0.9}
            for text in texts
        ]


class TestPredictBatchedRoute:
    def _monkeypatch_predictor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import api as api_module

        monkeypatch.setattr(
            api_module,
            "_get_predictor",
            lambda model_name=None: _RouteFakePredictor(),
        )

    def test_groupe_et_repond_en_batch(
        self, monkeypatch: pytest.MonkeyPatch, _nettoyage_infra
    ) -> None:
        from api import app
        from api.routes import predict as predict_route

        self._monkeypatch_predictor(monkeypatch)
        predict_route.reset_predict_batcher()
        try:
            with TestClient(app) as client:
                response = client.post(
                    "/predict/batched",
                    json={"texts": ["Bonjour", "le monde"], "use_batcher": True},
                    headers={"X-API-Key": "test-key"},
                )
        finally:
            predict_route.reset_predict_batcher()
        assert response.status_code == 200, response.text
        payload = response.json()
        assert [r["sentiment"] for r in payload["results"]] == [
            "positive",
            "positive",
        ]
        assert [r["text"] for r in payload["results"]] == ["Bonjour", "le monde"]

    def test_fallback_sans_batcher(
        self, monkeypatch: pytest.MonkeyPatch, _nettoyage_infra
    ) -> None:
        from api import app

        self._monkeypatch_predictor(monkeypatch)
        with TestClient(app) as client:
            response = client.post(
                "/predict/batched",
                json={"texts": ["Bonjour"], "use_batcher": False},
                headers={"X-API-Key": "test-key"},
            )
        assert response.status_code == 200, response.text
        assert response.json()["results"][0]["sentiment"] == "positive"

    def test_liste_vide_rejetee(self, _nettoyage_infra) -> None:
        from api import app

        with TestClient(app) as client:
            response = client.post(
                "/predict/batched",
                json={"texts": [], "use_batcher": True},
                headers={"X-API-Key": "test-key"},
            )
        assert response.status_code == 422

    def test_sans_cle_api_rejetee(self, _nettoyage_infra) -> None:
        from api import app

        with TestClient(app) as client:
            response = client.post(
                "/predict/batched",
                json={"texts": ["Bonjour"], "use_batcher": True},
            )
        assert response.status_code == 401

