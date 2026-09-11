#!/usr/bin/env python
"""Vérifie (CI bloquante) les pins digest Docker — P2 lot 15 (supply-chain).

Compare ``.docker/digest-pins.yml`` (commité) aux digests réellement publiés
sur Docker Hub (API ``hub.docker.com/v2/repositories/...``, endpoint public) :

    - chaque image épinglée doit exister avec un digest amd64 conforme ;
    - l'image SearXNG doit en plus correspondre au tag épinglé (pas de
      dérive de ``latest``) ;
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


def resolve_digest(image_ref: str) -> dict:
    """Résout `repo:tag` → {"digest", "tag"} depuis Docker Hub (manifest amd64).

    Lève ``RuntimeError`` si l'image ou le tag n'existe pas (fail-closed : on
    ne devine JAMAIS un digest).
    """
    if "/" not in image_ref:
        raise RuntimeError(f"référence image invalide : {image_ref!r} (attendu repo/image)")
    repo, _, tag = image_ref.partition(":")
    tag = tag or "latest"
    entries = _hub_tags(repo, tag)
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
            remote = resolve_digest(f"{image}:{pin.get('tag', 'latest')}")
        except RuntimeError as exc:
            errors.append(f"{image}: {exc}")
            continue
        if pin.get("digest") != remote["digest"]:
            errors.append(
                f"{image}: digest commité {pin.get('digest')} != Docker Hub "
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
        updated: dict[str, dict] = {}
        for image, _pin in images.items():
            resolved = resolve_digest(f"{image}:{_pin.get('tag', 'latest')}")
            updated[image] = {**_pin, **resolved}
        PINS_PATH.write_text(
            yaml.safe_dump({"images": updated}, sort_keys=False, allow_unicode=True),
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
