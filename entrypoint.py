#!/usr/bin/env python
"""
Entrypoint script pour ThinkTuning.
Permet de choisir entre entraînement, API ou les deux.

Usage:
    python entrypoint.py train   # Lancer l'entraînement
    python entrypoint.py api     # Lancer l'API
    python entrypoint.py both    # Lancer les deux
"""

import sys
import subprocess
import time
import threading

def run_training():
    """Lance l'entraînement."""
    print("[ThinkTuning] Lancement de l'entraînement...")
    subprocess.run([sys.executable, "train.py"])

def run_api():
    """Lance l'API FastAPI."""
    print("[ThinkTuning] Lancement de l'API...")
    subprocess.run(["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"])

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "api"
    
    if mode == "train":
        run_training()
    elif mode == "api":
        run_api()
    elif mode == "both":
        # Lancer l'entraînement dans un thread, puis l'API en foreground
        train_thread = threading.Thread(target=run_training, daemon=False)
        train_thread.start()
        
        # Petit délai pour laisser l'entraînement démarrer
        time.sleep(2)
        
        # Lancer l'API en foreground (blocking)
        run_api()
    else:
        print(f"Mode inconnu: {mode}")
        print("Modes disponibles: train, api, both")
        sys.exit(1)

if __name__ == "__main__":
    main()
