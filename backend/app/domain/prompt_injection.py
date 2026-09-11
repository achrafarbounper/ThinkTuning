"""Détecteur de prompt-injection (P2 durable, lot 17) — DOMAINE pur, stdlib.

Détecte les tentatives d'injection directe (l'utilisateur ordonne au modèle de
contourner ses instructions) et indirecte (un contenu externe — résultat
d'outil — tente de prendre le contrôle du système).

Méthode **heuristique déterministe** (aucun appel LLM, aucun coût réseau) :

    - patterns classiques  : « ignore previous instructions », « disregard
      above », « forget all rules », « you are now », « system prompt » … ;
    - harvesting           : « print the api key », « reveal the secret »,
      « expose the database password » ;
    - contournements       : instructions encodées (base64), délimiteurs
      système recopiés (« <|im_start|> », « system: »), re-définition de rôle ;
    - indirect             : instructions cachées dans une page/fichier.

Sortie = ``PromptInjectionReport`` (score 0..1, severity
none/low/medium/high, patterns matchés) — consommée par la policy de la
couche application (reject / warn / off) et par la red-team trimestrielle.

Règles d'or :
    1. jamais de réseau, pas de LLM, pas d'I/O (tests unitaires triviaux) ;
    2. déterministe : même entrée -> même verdict (audit rejouable) ;
    3. un pattern à fort signal (harvest/role_hijack) force ``high``.
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass, field
from typing import Literal

Severity = Literal["none", "low", "medium", "high"]

# ============================================================
# Patterns — regroupés par famille (ordre stable pour l'audit)
# ============================================================

# Famille 1 : override d'instructions (commandes de SURPASSEMENT du système).
_OVERRIDE_PATTERNS = (
    r"\bignore\s+(?:all\s+)?(?:previous|prior|above|earlier)\s+instructions\b",
    r"\bignore\s+(?:the\s+)?(?:above|previous)\s+(?:instructions?|prompts?|rules?)\b",
    r"\bdisregard\s+(?:all\s+)?(?:previous|prior|above)\s+(?:instructions?|rules?|prompts?)\b",
    r"\bforget\s+(?:all\s+)?(?:previous|prior|above)\s+(?:instructions?|rules?|prompts?)\b",
    r"\boverride\s+(?:your\s+)?(?:system\s+)?(?:instructions?|prompts?|rules?)\b",
    r"\bdo\s+not\s+(?:follow|heed)\s+(?:your\s+|the\s+)?(?:system\s+)?(?:instructions?|prompts?|rules?)\b",
    r"\bremove\s+(?:your\s+|the\s+)?(?:system\s+)?prompt\b",
    r"\byou\s+are\s+no\s+longer\s+(?:bound|constrained)\s+by\b",
    r"\bignore\s+(?:all\s+)?(?:safety|security)\s+(?:guards?|protocols?|constraints)\b",
    r"\b(?:oublie|ignore|ne\s+tiens\s+pas\s+compte)\s+(?:de\s+)?(?:tout|toutes)\s+(?:ce\s+qui\s+précède|les\s+instructions)\b",
    r"\b(?:oublie|ignore)\s+(?:tes|vos)\s+(?:instructions|consignes)\s+(?:système|systeme)\b",
)

# Famille 2 : prise de contrôle du rôle / re-définition du persona.
_ROLE_HIJACK_PATTERNS = (
    r"\b(?:you\s+are|act\s+as|now\s+act\s+as|pretend\s+to\s+be|you\s+will\s+now\s+act\s+as)\s+a?\s*(?:DAN|developer\s+mode|unrestricted\s+ai|jailbreak)\b",
    r"\b(?:ignore\s+your\s+programming|forget\s+your\s+programming)\b",
    r"\b(?:from\s+now\s+on\s+you\s+are|now\s+you\s+are)\s+an?\s+(?:unfiltered|uncensored|unrestricted)\b",
    r"\bsystem\s*(?:prompt|message|instructions?)\s*(?:is|:)\s*(?:to|now)\b",
    r"\bsystem\s+override\b",
    r"\b(?:new|updated)\s+system\s+(?:prompt|instructions?)\s*:",
    r"\b<\|im_start\|>\s*system\b",
    r"\b(?:vous\s+êtes|tu\s+es)\s+désormais\s+(?:un|une)\s+ia\s+sans\s+(?:limites|restrictions|censure)\b",
)

# Famille 3 : exfiltration / harvesting de secrets.
# Note (red-team 2026-Q3) : trois formes couvertes —
#   a) verbe de révélation + nom de secret (tolère un complément court
#      « the value of OPENROUTER_API_KEY ») ;
#   b) COMBO exfiltration : un nom de secret suivi d'une destination
#      ``https?://`` (harvest + transport dans la même phrase) ;
#   c) verbes d'ENVOI français (envoie/transmets/partage) + secret.
_SECRET_HARVEST_PATTERNS = (
    r"\b(?:reveal|print|leak|show|expose|extract|dump|give|share|display)\b[^.\n]{0,24}\b(?:api[\s_-]*key|secret|token|password|credential|openrouter_api_key|hf_token|store_encryption_key)\b",
    r"\b(?:print|show|repeat|echo)\s+(?:the\s+)?(?:content\s+of\s+)?(?:your\s+)?(?:system\s+)?(?:prompt|instructions)\b",
    r"\b(?:list|show|export)\s+(?:all|every)\s+(?:environment\s+)?(?:variables|secrets)\b",
    r"\b(?:api[\s_-]*key|token|secret|password|credential|clé|cle)\b[^.\n]{0,80}\bhttps?://\b",
    r"\b(?:sortir|affiche|montre|révèle|donne|extraire|exfiltre|récupère)\b[^.\n]{0,24}\b(?:clé|cle|secret|token|mots?\s+de\s+passe|mot\s+de\s+passe)\b",
    r"\b(?:envoie|transmets|partage|poste|transfère)\b[^.\n]{0,24}\b(?:clé|cle|key|token|secret)\b",
    r"\b(?:clé|cle)\s+privée\s+(?:de\s+)?(?:git|ssh|aws|gcp|azure)\b",
)

# Famille 4 : contournements encodés / obfuscation.
_ENCODED_PATTERNS = (
    r"\bdecode\s+(?:this|the\s+following)\s+(?:base64|hex|binary)\s+(?:string|blob)\s+and\s+(?:follow|execute|do)\b",
    r"\breverse\s+engineer\s+this\s+(?:base64|obfuscated)\s+and\s+obey\b",
    r"\banagrammes?\s+ou\s+lettres\s+dispersées\s+(?:dans\s+)?(?:le\s+)?texte\s+pour\s+contourner\b",
)

# Famille 5 : relais d'instructions depuis un contenu externe (indirect).
_INDIRECT_HIJACK_PATTERNS = (
    r"\bignore\s+(?:the\s+)?(?:instructions|rules)\s+in\s+(?:this|the)\s+(?:page|website|article|file|document)\b",
    r"\b(?:the\s+)?(?:page|website|file|text)\s+contains?\s+(?:instructions|a\s+prompt)\s+for\s+you\b",
    r"\bfollow\s+(?:the\s+)?instructions\s+in\s+(?:this|the)\s+(?:page|website|file|document|text)\s+.\s*these\s+are\s+(?:your\s+)?(?:new\s+)?(?:instructions|rules)\b",
    r"\b(?:according|per)\s+to\s+(?:the\s+)?(?:following|above|this)\s+(?:system|hidden)\s+instructions\b",
)

# Scores par famille (accumulés ; un pattern HARVEST/ROLE force high).
_OVERRIDE_WEIGHT = 0.65
_ROLE_HIJACK_WEIGHT = 0.75
_SECRET_HARVEST_WEIGHT = 0.9
_ENCODED_WEIGHT = 0.55
_INDIRECT_WEIGHT = 0.7

# Familles de détection (nom, patterns, poids) — consommées par
# ``detect_prompt_injection`` (compilation paresseuse des regex).
_FAMILY_SPECS: tuple[tuple[str, tuple[str, ...], float], ...] = (
    ("override", _OVERRIDE_PATTERNS, _OVERRIDE_WEIGHT),
    ("role_hijack", _ROLE_HIJACK_PATTERNS, _ROLE_HIJACK_WEIGHT),
    ("secret_harvest", _SECRET_HARVEST_PATTERNS, _SECRET_HARVEST_WEIGHT),
    ("encoded", _ENCODED_PATTERNS, _ENCODED_WEIGHT),
    ("indirect", _INDIRECT_HIJACK_PATTERNS, _INDIRECT_WEIGHT),
)

# Seuils de bascule severity (score cumulé, plafonné à 1.0).
_LOW_THRESHOLD = 0.35
_MEDIUM_THRESHOLD = 0.55

# Familles au signal FORT : un seul match => severity high (audit certain).
_HARD_HIGH_FAMILIES = frozenset({"secret_harvest", "role_hijack"})

# Coupure d'analyse (prompts géants légitimes : coût borné).
_MAX_ANALYZED_CHARS = 20000

# Blobs base64 significatifs (>= 40 chars) => suspicion d'encodage.
_B64_BLOB_RE = re.compile(r"[A-Za-z0-9+/]{40,}={0,2}(?:\s+[A-Za-z0-9+/]{40,}={0,2})*")


def _norm(text: str) -> str:
    """Normalise pour les patterns : minuscules + espaces uniformisés."""
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def _severity_for(score: float, hard_hit: bool) -> Severity:
    if hard_hit or score >= 0.9:
        return "high"
    if score >= _MEDIUM_THRESHOLD:
        return "medium"
    if score >= _LOW_THRESHOLD:
        return "low"
    return "none"


@dataclass(frozen=True)
class PromptInjectionReport:
    """Verdict déterministe du détecteur (auditable, sérialisable JSON)."""

    severity: Severity = "none"
    score: float = 0.0
    matched: tuple[str, ...] = field(default_factory=tuple)
    flagged: bool = field(default=False)

    def to_dict(self) -> dict:
        return {
            "severity": self.severity,
            "score": round(self.score, 3),
            "matched": list(self.matched),
            "flagged": self.flagged,
        }


def detect_prompt_injection(text: str) -> PromptInjectionReport:
    """Analyse un texte (prompt utilisateur OU résultat d'outil) pour des
    tentatives d'injection. Pure et déterministe.

    ``flagged=True`` quand ``severity >= medium`` — signal consommé par la
    policy applicative (reject / warn).
    """
    if not text or not str(text).strip():
        return PromptInjectionReport()
    raw = str(text)
    normalized = _norm(raw[:_MAX_ANALYZED_CHARS])

    score = 0.0
    matched: list[str] = []
    hard_hit = False

    # Familles (nom, patterns, poids) — définies hors fonction, compilées ici.
    families = (
        (name, re.compile("|".join(patterns), re.IGNORECASE), weight)
        for name, patterns, weight in _FAMILY_SPECS
    )
    for name, pattern, weight in families:
        found = pattern.findall(normalized)
        if found:
            score += weight * min(1.0, len(found))
            matched.append(f"{name}:{len(found)}")
            if name in _HARD_HIGH_FAMILIES:
                hard_hit = True

    # Encodage base64 massif : signal additionnel (blob >= 40 chars).
    compact = re.sub(r"\s", "", raw[:_MAX_ANALYZED_CHARS])
    b64_matches = _B64_BLOB_RE.findall(compact)
    if b64_matches:
        try:
            base64.b64decode(b64_matches[0], validate=True)
            score += 0.25
            matched.append("base64_blob")
        except (binascii.Error, ValueError):
            pass

    score = min(1.0, score)
    severity = _severity_for(score, hard_hit)
    return PromptInjectionReport(
        severity=severity,
        score=score,
        matched=tuple(matched),
        flagged=severity in ("medium", "high"),
    )


def is_suspicious(text: str) -> bool:
    """Raccourci booléen (policy) : True si flagged (>= medium)."""
    return detect_prompt_injection(text).flagged


def redact_suspicious_context(value: str, max_chars: int = 2000) -> str:
    """Prépare un contexte d'outil suspect pour réinjection LLM : tronqué.

    Défense anti-poisoning indirect : quand un résultat d'outil est jugé
    suspect (injection détectée dans son contenu), on n'injecte JAMAIS la
    totalité brute au modèle — un préfixe court + avertissement.
    """
    report = detect_prompt_injection(value)
    if report.severity == "none":
        return value
    snippet = " ".join(str(value).split())[:max_chars]
    return (
        f"[AVERTISSEMENT SECURITE : le contenu suivant est jugé suspect "
        f"({report.severity}, score {report.score:.2f}) et a été tronqué.] {snippet}"
    )


__all__ = [
    "PromptInjectionReport",
    "detect_prompt_injection",
    "is_suspicious",
    "redact_suspicious_context",
]
