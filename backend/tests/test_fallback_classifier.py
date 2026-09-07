"""Tests Phase 3 : règles de repli métier + classifieur résilient.

Vérifie que : (1) les règles lexicales retournent des labels déterministes ;
(2) ``FallbackClassifier`` implémente le contrat ``BaseClassifier`` sans
modèle ; (3) ``ResilientClassifier`` branche le ``CircuitBreaker`` existant
et bascule automatiquement sur le repli sans jamais appeler le modèle quand
le circuit est ouvert.
"""

from __future__ import annotations

import pytest

from ia.agent.circuit_breaker import CircuitBreaker
from ia.agent.classifiers.base import BaseClassifier, PredictionResult
from ia.agent.classifiers.fallback import (
    FallbackClassifier,
    ResilientClassifier,
    fallback_intent,
    fallback_sentiment,
)


class _InnerStub(BaseClassifier):
    """Classifieur interne simulé (aucun modèle chargé)."""

    name = "sentiment"

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0
        self.success_count = 0

    def load_model(self) -> None:
        return None

    def reload(self) -> None:
        return None

    def predict(self, texts: list[str]) -> list[PredictionResult]:
        self.calls += 1
        if self.fail:
            raise RuntimeError("modèle HS")
        self.success_count += 1
        return [
            PredictionResult(text=text, label="positive", confidence=0.9)
            for text in texts
        ]

    def health_check(self) -> dict:
        return {"ok": True, "engine": "stub"}

    def get_metrics(self) -> dict:
        return {"name": "sentiment", "engine": "stub"}


class TestFallbackRules:
    def test_sentiment_positif(self) -> None:
        label, confidence = fallback_sentiment("Ce produit est excellent et génial")
        assert label == "positive"
        assert confidence >= 0.62

    def test_sentiment_negatif(self) -> None:
        label, confidence = fallback_sentiment("C'était horrible et décevant")
        assert label == "negative"
        assert confidence >= 0.62

    def test_sentiment_neutre(self) -> None:
        label, confidence = fallback_sentiment("Le train part à 14h")
        assert label == "neutral"
        assert confidence == pytest.approx(0.6)
class TestFallbackClassifier:
    def test_predict_sentiment_contract(self) -> None:
        classifier = FallbackClassifier("sentiment")
        results = classifier.predict(["Super !", "Horrible.", "Reçu ce matin"])
        assert len(results) == 3
        assert [r.label for r in results] == ["positive", "negative", "neutral"]
        assert all(isinstance(r.confidence, float) for r in results)

    def test_predict_intent_contract(self) -> None:
        classifier = FallbackClassifier("intent")
        results = classifier.predict(["Merci beaucoup", "Cherche le rapport q3"])
        assert [r.label for r in results] == ["chat", "action"]

    def test_predict_empty(self) -> None:
        assert FallbackClassifier().predict([]) == []

    def test_nom_inconnu_rejete(self) -> None:
        with pytest.raises(ValueError):
            FallbackClassifier("quantum")

    def test_noop_vie(self) -> None:
        classifier = FallbackClassifier()
        classifier.load_model()
        classifier.reload()
        assert classifier.health_check()["ok"] is True

    def test_metrics_engine_rules(self) -> None:
        assert FallbackClassifier("sentiment").get_metrics()["engine"] == "rules"


class TestResilientClassifier:
    def test_succes_appelle_interne_et_enregistre(self) -> None:
        inner = _InnerStub()
        breaker = CircuitBreaker(failure_threshold=3, recovery_timeout=60.0)
        resilient = ResilientClassifier(inner, breaker=breaker)

        results = resilient.predict(["Excellent produit"])
        assert results[0].label == "positive"
        assert inner.calls == 1
        assert breaker.metrics["success_count"] == 1
        assert resilient.get_metrics()["fallback_count"] == 0

    def test_circuit_ouvert_utilise_repli_sans_modele(self) -> None:
        inner = _InnerStub()
        breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=60.0)
        breaker.record_failure()  # passe OPEN
        resilient = ResilientClassifier(inner, breaker=breaker)

        results = resilient.predict(["Tout est horrible"])
        assert results[0].label == "negative"  # repli règles
        assert inner.calls == 0  # le modèle n'est jamais sollicité
        assert resilient.get_metrics()["fallback_count"] == 1

    def test_echec_sous_seuil_relance(self) -> None:
        inner = _InnerStub(fail=True)
        breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=60.0)
        resilient = ResilientClassifier(inner, breaker=breaker)

        with pytest.raises(RuntimeError, match="modèle HS"):
            resilient.predict(["Texte"])
        # circuit pas encore ouvert : le fallback ne s'active pas
        assert resilient.get_metrics()["fallback_count"] == 0
        assert breaker.state == "closed"

    def test_echec_au_seuil_bascule_vers_repli(self) -> None:
        inner = _InnerStub(fail=True)
        breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=60.0)
        resilient = ResilientClassifier(inner, breaker=breaker)

        results = resilient.predict(["Texte"])
        assert results[0].label in {"positive", "negative", "neutral"}
        assert inner.calls == 1  # tentative réelle, puis repli
        assert breaker.state == "open"
        assert resilient.get_metrics()["fallback_count"] == 1

    def test_rechargement_reinitialise_le_circuit(self) -> None:
        inner = _InnerStub()
        breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=60.0)
        breaker.record_failure()  # OPEN
        resilient = ResilientClassifier(inner, breaker=breaker)

        resilient.reload()
        assert breaker.state == "closed"
        assert resilient.predict(["OK"])[0].label == "positive"

    def test_health_check_delegue(self) -> None:
        resilient = ResilientClassifier(_InnerStub())
        assert resilient.health_check()["engine"] == "stub"

    def test_metrics_agregees(self) -> None:
        inner = _InnerStub()
        resilient = ResilientClassifier(inner)
        resilient.predict(["Un"])
        metrics = resilient.get_metrics()
        assert metrics["name"] == "resilient-sentiment"
        assert metrics["fallback_count"] == 0
        assert metrics["circuit"]["state"] == "closed"

    def test_get_model_info_expose_circuit(self) -> None:
        resilient = ResilientClassifier(_InnerStub())
        info = resilient.get_model_info()
        assert info["name"] == "resilient-sentiment"
        assert info["circuit"] == "closed"

    def test_intent_action(self) -> None:
        label, _ = fallback_intent("Peux-tu lancer l'entraînement du modèle ?")
        assert label == "action"

    def test_intent_chat(self) -> None:
        label, _ = fallback_intent("Tu peux m'expliquer ce que fait ce projet ?")
        assert label == "chat"

    def test_faux_positif_court_evite(self) -> None:
        # « top » dans « stop » ou « donne » dans « données » : pas d'action.
        label, _ = fallback_intent("Quelles données as-tu déjà vues ?")
        assert label == "chat"
