"""Masquage de secrets dans des chaînes / structures (P1 SEC, point 10c).

Complémentaire du ``redact()`` par CLÉS d'``audit_store`` (qui écrasent une
valeur sous une clé nommée ``api_key``/``token``/…), ce module masque les
secrets qui apparaissent dans le TEXTE des valeurs : une chaîne ``sqlite``
de connexion, un ``postgresql://user:pass@host``, un ``Authorization: Bearer
…``, un ``API_KEY=sk-abc`` stocké dans un prompt ou la réponse d'un outil.

Règles (appliquées dans cet ordre, insensibles à la casse) :
    1. DSN : ``postgres(ql)://<credentials>@`` -> ``postgresql://***@`` ;
    2. Bearer : ``Bearer <token>``                 -> ``Bearer ***`` ;
    3. paire clé=valeur : ``API_KEY=sk-abc``        -> ``API_KEY=***``
       (la clé porteuse d'un suffixe _key/_token/_secret/dsn/password) ;
    4. mot porteur isolé (sans valeur attachée)     -> ``***``
       (ex. une phrase citant « API_KEY » sans sa valeur).

Rien d'autre n'est modifié : le contexte lisible est conservé pour l'audit.
"""

from __future__ import annotations

import re
from typing import Any

# 1. Chaîne de connexion PostgreSQL : masque TOUT entre le schéma et le `@`.
_DSN_RE = re.compile(r"(?i)(postgres(?:ql)?://)[^@/]+@")
# 2. Jeton HTTP porteur (Authorization: Bearer xxxx).
_BEARER_RE = re.compile(r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/=\-]+")

# Clé porteuse d'un secret. Deux familles :
#   - COMPOSÉE  : tout mot contenant _key / _token / _secret / _dsn /
#     _password (API_KEY, HF_TOKEN, AGENT_PG_DSN, openrouter_api_key…) ;
#   - EXACTE    : mot seul parmi les noms canoniques (token, secret, key
#     N'apparaît pas seul — trop générique — mais api_key/apikey oui).
_COMPOUND_KEY = r"\w*(?:_key|_token|_secret|_dsn|_password)\b"
_EXACT_KEY = r"(?:api_?key|apikey|token|secret|password|passwd|pwd|authorization|dsn)"
_SENSITIVE_KEY = rf"(?:{_COMPOUND_KEY}|{_EXACT_KEY})"

# 3. Paire clé=valeur : la VALEUR (jusqu'au premier séparateur espace/virgule/
#    point-virgule/quote) est masquée.
_KV_RE = re.compile(
    r"(?i)(\b(?:"
    + _SENSITIVE_KEY
    + r")\s*[=:]\s*[\"']?)[^\"'\s,;]+"
)
# 4. Mot porteur SANS valeur attachée (pas suivi de `=`/`:` avec valeur).
_BARE_KEY_RE = re.compile(rf"(?i)\b(?:{_SENSITIVE_KEY})\b(?!\s*[=:])")


def _redact_text(text: str) -> str:
    """Masque les secrets d'une chaîne (règles 1→4, ordre figé)."""
    text = _DSN_RE.sub(r"\1***@", text)
    text = _BEARER_RE.sub(r"\1***", text)
    text = _KV_RE.sub(r"\1***", text)
    text = _BARE_KEY_RE.sub("***", text)
    return text


def redact_secrets(value: Any) -> Any:
    """Masque récursivement les secrets d'une valeur (dict/list/scalaire).

    Les dict et list sont parcourus en profondeur ; les chaînes passent par
    ``_redact_text`` ; ``None`` et les scalaires typés sont inchangés.
    """
    if isinstance(value, dict):
        return {key: redact_secrets(val) for key, val in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_secrets(item) for item in value]
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return _redact_text(str(value))


__all__ = ["redact_secrets"]
