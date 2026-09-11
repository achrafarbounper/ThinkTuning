#!/usr/bin/env python
"""Vérifie (CI bloquante) les pins digest Docker — P2 lot 15 (supply-chain).

Compare ``.docker/digest-pins.yml`` (commité) aux digests réellement publiés
sur Docker Hub (API ``hub.docker.com/v2/repositories/...``, endpoint public) :

    - chaque image épinglée doit exister avec un digest amd64 conforme ;
    - le tag est explicite (champ ``tag`` de l'entrée, ou tag embarqué dans la
      clé) — aucun repli silencieux sur ``latest`` (fail-closed) ;
    - les images officielles (``python``, ``node``, ``nginx``...) sont
      résolues sous le namespace Hub ``library/<image>`` ;
    - exit code 1 dès le premier écart (job CI bloquant High/Critical : un pin
      cassé signifie que l'image référencée a changé — risque de supply-chain).

Usage :
    python scripts/check_docker_pins.py            # vérifie (CI bloquante)
    python scripts/check_docker_pins.py --refresh  # met à jour le fichier
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]  # racine du repo (backend/scripts -> ../..)
PINS_PATH = ROOT / ".docker" / "digest-pins.yml"
HUB_API = "https://hub.docker.com/v2/repositories/{repo}/tags?page_size=100&name={name}"

# En-tête réécrit par --refresh (safe_dump écraserait sinon les commentaires).
REFRESH_HEADER = """\
# P2 lot 15 — Pin digest Docker (supply-chain).
#
# Images externes (Dockerfiles / docker-compose), épinglées par digest SHA-256
# du manifeste amd64 (résolu via l'API Docker Hub par --refresh). Le job CI
# "supply-chain" (scripts/check_docker_pins.py) REFUSE tout écart entre ces
# pins et l'état publié du registre : une image re-taguée ou compromise fait
# échouer la pipeline bloquante.
#
# Style canonique : clé = repo, tag explicite dans l'entrée (le script refuse
# toute entrée sans tag — jamais de repli silencieux sur 'latest').
#
# Procédure de mise à jour :
#   python scripts/check_docker_pins.py --refresh   # réécrit ce fichier
"""


def _hub_tags(repo: str, name: str) -> list[dict]:
    url = HUB_API.format(repo=repo, name=name)
    with urllib.request.urlopen(url, timeout=20) as resp:  # noqa: S310 - Docker Hub public
        payload = json.loads(resp.read().decode("utf-8"))
    return payload.get("results", [])


def _amd64_digest(entry: dict) -> str | None:
    for image in entry.get("images", []):
        if image.get("architecture") == "amd64":
            return image.get("digest")
    return None


def _hub_repo(repo: str) -> str:
    """Namespace Hub : les images officielles vivent sous ``library/``."""
    return repo if "/" in repo else f"library/{repo}"


def parse_pin(image: str, pin: dict) -> tuple[str, str]:
    """Décompose une entrée du fichier de pins → ``(repo, tag)``.

    Deux styles acceptés (aucun tag ni digest n'est deviné — fail-closed) :

        python:            {tag: 3.13-slim, digest: ...}  # champ explicite
        python:3.13-slim:  {digest: ...}                  # tag embarqué

    Si les deux coexistent ils doivent concorder ; sans aucun des deux,
    l'entrée est invalide (jamais de repli sur ``latest``).
    """
    repo, _, embedded = image.partition(":")
    explicit = pin.get("tag")
    if embedded and explicit and embedded != explicit:
        raise RuntimeError(
            f"{image}: champ 'tag' {explicit!r} != tag embarqué {embedded!r}"
        )
    tag = explicit or embedded
    if not tag:
        raise RuntimeError(f"{image}: aucun tag précisé (champ 'tag' attendu)")
    return repo, tag


def resolve_digest(image_ref: str) -> dict:
    """Résout ``repo:tag`` → ``{"digest", "tag"}`` depuis Docker Hub (amd64).

    Lève ``RuntimeError`` si l'image ou le tag n'existe pas (fail-closed : on
    ne devine JAMAIS un digest).
    """
    repo, _, tag = image_ref.partition(":")
    if not repo or not tag:
        raise RuntimeError(f"référence image invalide : {image_ref!r} (attendu repo:tag)")
    entries = _hub_tags(_hub_repo(repo), tag)
    exact = next((e for e in entries if e.get("name") == tag), None)
    if exact is None:
        raise RuntimeError(f"tag introuvable sur Docker Hub : {repo}:{tag}")
    digest = _amd64_digest(exact)
    if digest is None:
        raise RuntimeError(f"aucun digest amd64 pour {repo}:{tag}")
    return {"digest": digest, "tag": exact.get("name")}


def check_pins(pins: dict) -> list[str]:
    """Compare les pins commités aux valeurs Docker Hub. Retourne les écarts."""
    errors: list[str] = []
    for image, pin in pins.items():
        try:
            repo, tag = parse_pin(image, pin or {})
            remote = resolve_digest(f"{repo}:{tag}")
        except RuntimeError as exc:
            errors.append(f"{image}: {exc}")
            continue
        if (pin or {}).get("digest") != remote["digest"]:
            errors.append(
                f"{image}: digest commité {(pin or {}).get('digest')} != Docker Hub "
                f"{remote['digest']} (tag {remote['tag']}) — re-pinnez via --refresh"
            )
    return errors


def main() -> int:
    pins = yaml.safe_load(PINS_PATH.read_text(encoding="utf-8")) or {}
    images = pins.get("images", {})
    if not images:
        print(f"aucune image épinglée dans {PINS_PATH}")
        return 1

    if "--refresh" in sys.argv:
        # Style canonique : clé = repo (sans tag embarqué), tag porté par le
        # champ dédié → fichier auto-descriptif, un seul style de parsing.
        updated: dict[str, dict] = {}
        for image, _pin in images.items():
            repo, tag = parse_pin(image, _pin or {})
            resolved = resolve_digest(f"{repo}:{tag}")
            updated[repo] = {"tag": resolved["tag"], "digest": resolved["digest"]}
        PINS_PATH.write_text(
            REFRESH_HEADER
            + yaml.safe_dump({"images": updated}, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        print(f"{len(updated)} pin(s) actualisé(s) dans {PINS_PATH}")
        return 0

    errors = check_pins(images)
    if errors:
        for err in errors:
            print(f"[FAIL] {err}")
        return 1
    print(f"[OK] {len(images)} pin(s) Docker conforme(s) à Docker Hub")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
