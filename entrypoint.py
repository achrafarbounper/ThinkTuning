#!/usr/bin/env python
"""
Entrypoint script pour ThinkTuning.
Dans OPTION B, supervisord gère Nginx + Uvicorn.
Ce script ne sert plus qu'à lancer l'entraînement si demandé.
"""

import subprocess
import sys

def run_training():
    print("[ThinkTuning] Lancement de l'entraînement...")
    subprocess.run([sys.executable, "train.py"])

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "api"

    if mode == "train":
        run_training()
    elif mode == "api":
        print("[ThinkTuning] Mode API — supervisord gère Uvicorn.")
    elif mode == "full":
        print("[ThinkTuning] Mode full — supervisord gère API + frontend.")
    elif mode == "both":
        print("[ThinkTuning] Mode both — supervisord gère API + frontend.")
    else:
        print(f"Mode inconnu: {mode}")
        print("Modes disponibles: train, api, full, both")
        sys.exit(1)

if __name__ == "__main__":
    main()
