"""Tests de la réparation des doubles-encodages UTF-8 → Latin-1 → UTF-8.

Couvre ``ia/agent/encoding.repair_utf8_mojibake`` :
  - mojibake simple (``tête`` → relu Latin-1 ``tÃªte``) ;
  - mojibake double (``tête`` → ``tÃ\x83Âªte``) ;
  - mojibake triple / accumulé ou suivant plusieurs relectures ;
  - textes légitimes (ASCII, accents Latin-1, polonais, emojis) intacts ;
  - chaînes vides / sans mojibake inchangées.
"""

import pytest

from ia.agent.encoding import _has_ctrl, repair_utf8_mojibake


def _mojibake_once(s: str) -> str:
    """Relit une chaîne UTF-8 comme si elle avait été transcodée en Latin-1."""
    return s.encode("utf-8").decode("latin-1")


def test_ascii_unchanged():
    assert repair_utf8_mojibake("Bonjour le monde ! #123") == "Bonjour le monde ! #123"


def test_empty_unchanged():
    assert repair_utf8_mojibake("") == ""


def test_latin1_legitimate_unchanged():
    """Ces caractères sont des accents Latin-1 / Unicode légitimes : ne jamais
    les « réparer » en faux positif."""
    for value in ("café", "coração", "crianças", "Größe", "naïve", "Résumé",
                  "ÉÈÊ légitime", "regarde — tiret", "déjà"):
        assert repair_utf8_mojibake(value) == value


def test_simple_mojibake_repaired():
    original = "Bonjour, tête désolée. À l'aide !"
    mojibake = _mojibake_once(original)
    assert mojibake != original  # bien un mojibake
    assert repair_utf8_mojibake(mojibake) == original


def test_double_mojibake_repaired():
    original = "Rebonjour ! Souhaitez-vous continuer l'exploration ?"
    mojibake = _mojibake_once(_mojibake_once(original))
    assert repair_utf8_mojibake(mojibake) == original


def test_triple_mojibake_repaired():
    original = "tête â é è à ç î ï"
    mojibake = _mojibake_once(_mojibake_once(_mojibake_once(original)))
    assert repair_utf8_mojibake(mojibake) == original


def test_emoji_and_accents_repaired():
    """Emojis (4 octets) et accents : le mojibake Africa tout aussi bien."""
    original = "Bonjour 👋 Gérer des tâches 🔍"
    mojibake = _mojibake_once(original)
    assert repair_utf8_mojibake(mojibake) == original


def test_real_world_ticket_example():
    # Valeur réellement persistée dans experiments/agent_sessions.db (id 216).
    corrupted = (
        "RebonjourÃ¢Â\x80Â¯! Content de vous revoir. "
        "SouhaitezÃ¢Â\x80Â\x91vous continuer lÃ¢Â\x80Â\x99exploration "
        "du modÃ\x83Â¨le de prÃ\x83Â©diction de sentiment"
    )
    assert repair_utf8_mojibake(corrupted) == (
        "Rebonjour\u202f! Content de vous revoir. "
        "Souhaitez\u2011vous continuer l\u2019exploration "
        "du modèle de prédiction de sentiment"
    )


def test_no_false_positive_on_valid_accents_sequences():
    # « Ã » + « © » est un vrai mojibake, mais un texte pouvant légitimement
    # contenir la séquence ne doit jamais être cassé : la passe échoue si le
    # résultat final contient des contrôles, et on garde l'entrée.
    assert not _has_ctrl(repair_utf8_mojibake("rÃ©sumÃ©"))


def test_result_never_contains_ctrl_residue():
    for original in ("accès", "créé", "à côté", "prêt", "décembre", "ambiguë"):
        out = repair_utf8_mojibake(_mojibake_once(original))
        assert not _has_ctrl(out)
        assert out == original