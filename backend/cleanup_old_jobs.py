#!/usr/bin/env python3
"""Nettoie les anciens jobs d'entraînement terminés.

Exemples :
    py cleanup_old_jobs.py --max-age-days 30 --dry-run
    py cleanup_old_jobs.py --max-age-days 30
    py cleanup_old_jobs.py --db-path experiments/jobs.db --max-age-days 7
"""

import argparse
import logging
import os
import sys

os.environ.setdefault("API_KEY", os.getenv("API_KEY", "local-cleanup"))

from api import cleanup_old_jobs

logger = logging.getLogger(__name__)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(name)s | %(message)s")
    parser = argparse.ArgumentParser(description="Supprime les jobs d'entraînement terminés et obsolètes.")
    parser.add_argument(
        "--db-path",
        default=os.getenv("JOB_STORE_PATH", os.path.join("experiments", "jobs.db")),
        help="Chemin SQLite du store de jobs (défaut: JOB_STORE_PATH ou experiments/jobs.db)",
    )
    parser.add_argument(
        "--max-age-days",
        type=int,
        default=30,
        help="Âge maximal des jobs terminés à conserver, en jours (défaut: 30)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Affiche les jobs qui seraient supprimés sans les supprimer réellement",
    )
    args = parser.parse_args()

    result = cleanup_old_jobs(
        max_age_days=args.max_age_days,
        dry_run=args.dry_run,
        db_path=args.db_path,
    )

    if args.dry_run:
        logger.info(f"[dry-run] {len(result['job_ids'])} jobs obsolètes détectés pour un seuil de {args.max_age_days} jours.")
    else:
        logger.info(f"{result['deleted']} job(s) supprimé(s) pour un seuil de {args.max_age_days} jours.")

    if result["job_ids"]:
        logger.info("job_ids: " + ", ".join(result["job_ids"]))
    else:
        logger.info("Aucun job obsolète à supprimer.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
