"""Réparation des doubles-encodages UTF-8 → Latin-1 → UTF-8 (mojibake).

Quand une réponse LLM arrive sur un canal qui a déjà été interprété en
Latin-1/ISO-8859-1 alors que le flux était en réalité en UTF-8, chaque
caractère UTF-8 d'origine est « éclaté » en plusieurs caractères Latin-1.
Exemple réel : ``tête`` devient ``tÃªte`` (simple) ou ``tÃ\x83Âªte``
(double, après passage dans un schéma ou un prompt).

Ce module fournit ``repair_utf8_mojibake`` : une réparation itérative,
déterministe et conservatrice qui ne touche jamais aux chaînes légitimes
(ASCII, accents latin-1, « café », « coração », « Größe »…).

Le module ne dépend que de la bibliothèque standard, sans aucun import vers
le reste du paquet ou du projet : il peut donc être importé par
``core.session_store`` / ``api.routes`` sans risque d'import circulaire.
"""

from __future__ import annotations

# Caractères qui n'apparaissent JAMAIS dans un contenu LLM français/anglais
# sain mais qui signalent un texte UTF-8 relu comme Latin-1/ISO-8859-1 :
#   À Â â     (0xC3 0x82 → « À » etc., préfixes de séquences UTF-8 relues)
#   – ’ “ … €  (0xE2 … → préfixes 3-octets)
#   ™         (0xE2 0x84 0xA2 → « ™ » dans le programme de remplacement)
# La présence de l'un de ces caractères est un marqueur fort de mojibake.
_MOJIBIKE_MARKERS = set("\u00c3\u00c2\u00e2\u20ac\u2122\u2026")

# Contrôles qu'un texte final légitime ne doit pas contenir : C0 hors sauts
# de ligne/tabulation, tous les contrôles C1 (0x80-0x9F, résidus d'une
# décombinaison partielle) et le caractère de remplacement U+FFFD.
_CONTROL_IGNORED = set("\n\r\t")


def _has_ctrl(text: str) -> bool:
    """Vrai si ``text`` contient un contrôle résiduel (C0 hors sauts de
    ligne, C1, ou U+FFFD) qui trahit une décombinaison incomplète."""
    for ch in text:
        code = ord(ch)
        if ch in _CONTROL_IGNORED:
            continue
        if (code < 0x20) or (0x7F <= code <= 0x9F) or code == 0xFFFD:
            return True
    return False


def _mozibake_once(text: str) -> str:
    """Tente une seule passe de décodage ``latin-1 -> utf-8``.

    Renvoie ``text`` inchangé si la passe n'apporte rien, produirait un
    résultat identique, ou échoue (contenu non représentable en Latin-1,
    donc légitime).
    """
    # Aucun marqueur → texte déjà sain, inutile d'essayer.
    if not text or not any(ch in text for ch in _MOJIBIKE_MARKERS):
        return text
    try:
        fixed = text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        # Impossible de relire comme Latin-1 → pas de mojibake, on garde.
        return text
    # Les caractères C1 du résultat direct (0x80-0x9F) peuvent être le fruit
    # d'une passe intermédiaire légitime ; on ne refuse donc ici que si le
    # résultat est strictement identique. La validation finale (C0/C1/FFFD)
    # est faite après la boucle dans ``repair_utf8_mojibake``.
    return text if fixed == text else fixed


def repair_utf8_mojibake(text: str) -> str:
    """Répare un/des double(s)-encodage(s) UTF-8 relu en Latin-1.

    Le décodage est appliqué itérativement jusqu'à stabilité, ce qui :
      - gère le mojibake simple  (``tête`` → ``tÃªte``)
      - gère le mojibake double  (``tête`` → ``tÃ\x83Âªte``)
    La réparation est conservatrice : si le résultat final contient encore
    des contrôles C0 (hors retours à la ligne) / C1 / U+FFFD, on considère
    qu'aucune décombinaison complète n'a eu lieu et on renvoie l'entrée
    d'origine inchangée (protection des textes Latin-1 légitimes).
    """
    if not text:
        return text
    previous = text
    current = _mozibake_once(text)
    while current != previous:
        previous = current
        current = _mozibake_once(current)
    if _has_ctrl(current):
        return text
    return current