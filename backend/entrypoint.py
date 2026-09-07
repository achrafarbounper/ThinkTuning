#!/usr/bin/env python
"""
Entrypoint script pour ThinkTuning.
Dans OPTION B, supervisord gère Nginx + Uvicorn.
Ce script ne sert plus qu'à lancer l'entraînement si demandé.
"""

import logging
import subprocess
import sys

logger = logging.getLogger(__name__)


def run_training():
    logger.info("[ThinkTuning] Lancement de l'entraînement...")
    subprocess.run([sys.executable, "train.py"])


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(name)s | %(message)s")
    mode = sys.argv[1] if len(sys.argv) > 1 else "api"

    if mode == "train":
        run_training()
    elif mode == "api":
        logger.info("[ThinkTuning] Mode API — supervisord gère Uvicorn.")
    elif mode == "full":
        logger.info("[ThinkTuning] Mode full — supervisord gère API + frontend.")
    elif mode == "both":
        logger.info("[ThinkTuning] Mode both — supervisord gère API + frontend.")
    else:
        logger.warning("Mode inconnu: %s", mode)
        logger.warning("Modes disponibles: train, api, full, both")
        sys.exit(1)

if __name__ == "__main__":
    main()
