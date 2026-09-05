"""Tests Phase 4 : classifieur d'intention (chat/action).

Cible :
  - ``IntentClassifier`` : repli règles quand aucun modèle entraîné (moteur
    ``auto``), moteur ``rules`` forcé, seuil de sécurité (une ``action`` sous
    le seuil perd le tranchage), validation des paramètres ;
  - ``core.intent_store`` : distinction dernière version vs pointeur actif ;
  - intégration OBSERVATOIRE dans le noyau v2 (``AgentCore.last_intent`` et
    événement ``agent.intent_detected``) sans altération de la boucle LLM.

Aucune dépendance lourde ni réseau : le classifieur modèle (torch/transformers)
n'est jamais chargé ici — on teste les chemins ``rules`` et l'absence de modèle.
"""

from __future__ import annotations

import json
import threading

import pytest

from core.intent_store import (
    default_intent_labels,
    resolve_intent_model_dir,
    set_active_intent_version,
)
from ia.agent.classifiers.intent_classifier import (
    IntentClassifier,
    resolve_intent_model_optional,
)


class TestIntentClassifierRules:
    def test_engine_rules_detecte_action(self) -> None:
        classifier = IntentClassifier(engine="rules")
        results = classifier.predict([
            "Peux-tu lancer l'entraînement du modèle ?",
            "Quelles sont tes capacités ?",
        ])
        assert [r.label for r in results] == ["action", "chat"]
        assert all(0.0 <= r.confidence <= 1.0 for r in results)

    def test_engine_auto_sans_modele_bascule_sur_regles(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr("core.intent_store.INTENT_MODEL_ROOT", str(tmp_path))
        monkeypatch.setattr(
            "ia.agent.classifiers.intent_classifier._MODEL_MISSING_WARNED", False
        )
        classifier = IntentClassifier(engine="auto")
        results = classifier.predict(["Peux-tu lancer l'entraînement du modèle ?"])
        assert results[0].label == "action"

    def test_seuil_securite_action_basculée_en_chat(self) -> None:
        classifier = IntentClassifier(engine="rules", threshold=0.99)
        results = classifier.predict(["Supprime le fichier"])
        assert results[0].label == "chat"  # confiance < seuil -> chat (sécurité)

    def test_seuil_zero_conserve_action(self) -> None:
        classifier = IntentClassifier(engine="rules", threshold=0.0)
        results = classifier.predict(["Liste les fichiers du dossier"])
        assert results[0].label == "action"

    def test_predict_empty(self) -> None:
        assert IntentClassifier(engine="rules").predict([]) == []

    def test_reload_et_health_check(self) -> None:
        classifier = IntentClassifier(engine="rules")
        classifier.reload()  # ne doit pas lever
        report = classifier.health_check()
        assert report["ok"] is True
        assert report["label"] in default_intent_labels()

    def test_get_model_info_signale_rules(self) -> None:
        info = IntentClassifier(engine="rules").get_model_info()
        assert info["engine"] == "rules"
        assert info["labels"] == list(default_intent_labels())

    def test_validation_engine(self) -> None:
        with pytest.raises(ValueError):
            IntentClassifier(engine="tensorflow")

    def test_validation_threshold(self) -> None:
        with pytest.raises(ValueError):
            IntentClassifier(engine="rules", threshold=1.5)


# ---------------------------------------------------------------------------
# core.intent_store
# ---------------------------------------------------------------------------


class TestIntentStore:
    def test_default_labels(self) -> None:
        assert default_intent_labels() == ("chat", "action")

    def test_resolve_aucun_modele_leve_erreur_claire(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr("core.intent_store.INTENT_MODEL_ROOT", str(tmp_path))
        with pytest.raises(RuntimeError, match="Aucun modèle d'intention"):
            resolve_intent_model_dir()

    @pytest.fixture()
    def _fake_model_dir(self, monkeypatch, tmp_path) -> str:
        """Crée une version valide + pointeur actif, isole le store sur tmp."""
        version = "20260901T120000Z"
        model_dir = tmp_path / version
        model_dir.mkdir(parents=True)
        (model_dir / "config.json").write_text(
            json.dumps({"_name_or_path": "mini-fake", "num_labels": 2}),
            encoding="utf-8",
        )
        (model_dir / "model.safetensors").write_text("weights", encoding="utf-8")
        (tmp_path / "active.json").write_text(
            json.dumps({"active": version}), encoding="utf-8"
        )
        monkeypatch.setattr("core.intent_store.INTENT_MODEL_ROOT", str(tmp_path))
        return version

    def test_resolve_pointeur_actif(self, _fake_model_dir) -> None:
        resolved = resolve_intent_model_dir()
        assert resolved.endswith("20260901T120000Z")

    def test_resolve_nom_explicite(self, _fake_model_dir) -> None:
        resolved = resolve_intent_model_dir("20260901T120000Z")
        assert "20260901T120000Z" in resolved

    def test_set_active_et_resolve(self, _fake_model_dir, tmp_path) -> None:
        v2 = tmp_path / "20260902T000000Z"
        v2.mkdir(parents=True)
        (v2 / "config.json").write_text(
            json.dumps({"_name_or_path": "mini-fake"}), encoding="utf-8"
        )
        (v2 / "model.safetensors").write_text("weights2", encoding="utf-8")
        set_active_intent_version("20260902T000000Z")
        assert resolve_intent_model_dir().endswith("20260902T000000Z")
        with open(tmp_path / "active.json", encoding="utf-8") as fh:
            assert json.load(fh)["active"] == "20260902T000000Z"

    def test_resolve_optional_retourne_none_sans_modèle(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr("core.intent_store.INTENT_MODEL_ROOT", str(tmp_path))
        assert resolve_intent_model_optional("400_check") is None
        with pytest.raises(ValueError):
            IntentClassifier(engine="rules", threshold=1.5)
# ---------------------------------------------------------------------------
# Intégration observatoire dans le noyau v2
# ---------------------------------------------------------------------------


class _FixedClassifier:
    """Classifieur factice : réponse fixe (label/confiance) + threadsafe."""

    name = "intent"

    def __init__(self, label: str, confidence: float = 0.95) -> None:
        self._label = label
        self._confidence = confidence
        self.calls = 0
        self._lock = threading.Lock()

    def predict(self, texts: list[str]) -> list[object]:
        with self._lock:
            self.calls += 1
        return [_FixedResult(t, self._label, self._confidence) for t in texts]


class _FixedResult:
    def __init__(self, text: str, label: str, confidence: float) -> None:
        self.text = text
        self.label = label
        self.confidence = confidence


class _ScriptedLLM:
    def __init__(self, reply: str = "Réponse textuelle simple.") -> None:
        self.reply = reply
        self.calls = 0

    def call(self, messages):
        self.calls += 1
        return self.reply

    def call_stream(self, messages, on_thinking=None, on_content=None):
        return self.call(messages)


class _Registry:
    def tool_names(self):
        return ["now", "echo"]

    def get(self, tool):
        if tool == "now":
            return lambda: "2026-09-02T12:00:00Z"
        if tool == "echo":
            return lambda text="": text
        return None

    def meta(self, tool):
        return {"description": f"outil {tool}", "required_args": []}


class _Bus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, event_type: str, **payload) -> None:
        self.events.append((event_type, payload))


class TestAgentCoreIntentIntegration:
    def test_last_intent_rempli_et_evenement_emis(self) -> None:
        from app.agent.core import AgentCore
        from app.domain.entities.plan import Intent

        classifier = _FixedClassifier("action", 0.9)
        bus = _Bus()
        core = AgentCore(_ScriptedLLM(), _Registry(), intent_classifier=classifier)
        core._event_bus = bus  # type: ignore[assignment]

        result = core.run(Intent(prompt="Lance l'entraînement", session_id="s1"))
        assert result.answer
        assert core.last_intent == {"label": "action", "confidence": 0.9}
        assert classifier.calls == 1
        topics = [t for t, _ in bus.events]
        assert "agent.intent_detected" in topics
        emitted = [p for t, p in bus.events if t == "agent.intent_detected"][0]
        assert emitted == {"label": "action", "confidence": 0.9}

    def test_sans_classifieur_comportement_inchange(self) -> None:
        from app.agent.core import AgentCore
        from app.domain.entities.plan import Intent

        core = AgentCore(_ScriptedLLM(), _Registry())
        result = core.run(Intent(prompt="Salut", session_id="s1"))
        assert result.answer
        assert core.last_intent is None

    def test_classifieur_en_echec_ne_casse_pas_le_run(self) -> None:
        from app.agent.core import AgentCore
        from app.domain.entities.plan import Intent

        class _Boom:
            def predict(self, texts):
                raise RuntimeError("modèle crashé")

        core = AgentCore(_ScriptedLLM(), _Registry(), intent_classifier=_Boom())
        result = core.run(Intent(prompt="Bonjour", session_id="s1"))
        assert result.answer
        assert core.last_intent is None  # échec avalé, run intact
