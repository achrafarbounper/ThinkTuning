"""Outils GPU de l'agent : VRAM et utilisation.

Deux sources complémentaires :
    1. torch.cuda (déjà une dépendance du projet) : nom, VRAM totale, mémoire
       allouée/réservée par processus PyTorch ;
    2. `nvidia-smi` (si installé avec le pilote) : utilisation % et mémoire
       réellement consommée côté pilote.

Fonctionne aussi sans GPU : renvoie un état explicite au lieu de lever.
"""

from tools.sandbox import run_subprocess


def _query_nvidia_smi() -> dict[int, dict]:
    """Interroge nvidia-smi ; retourne {index: {utilisation_percent, memory_used_mb, memory_total_mb}}."""
    code, out, _err = run_subprocess(
        [
            "nvidia-smi",
            "--query-gpu=index,name,utilization.gpu,memory.used,memory.total",
            "--format=csv,noheader,nounits",
        ],
        timeout=10,
        max_output_chars=4000,
    )
    if code != 0 or not out.strip():
        return {}

    stats: dict[int, dict] = {}
    for line in out.splitlines():
        parts = [chunk.strip() for chunk in line.split(",")]
        if len(parts) != 5:
            continue
        try:
            index = int(parts[0])
            stats[index] = {
                "smi_name": parts[1],
                "utilization_percent": int(parts[2]),
                "memory_used_mb": int(parts[3]),
                "memory_total_mb": int(parts[4]),
            }
        except ValueError:
            continue  # ligne non numérique (N/A, etc.)
    return stats


def gpu_info() -> dict:
    """État GPU complet : disponibilité CUDA, VRAM, utilisation."""
    info: dict = {
        "torch_available": False,
        "cuda_available": False,
        "devices": [],
        "source": [],
    }

    # 1) Vue PyTorch ------------------------------------------------------------
    try:
        import torch
    except ImportError:
        torch = None
    if torch is not None:
        info["torch_available"] = True
        info["torch_version"] = torch.__version__
        if torch.cuda.is_available():
            info["cuda_available"] = True
            info["cuda_version"] = torch.version.cuda
            info["source"].append("torch")
            for i in range(torch.cuda.device_count()):
                props = torch.cuda.get_device_properties(i)
                device = {
                    "index": i,
                    "name": props.name,
                    "total_memory_gb": round(props.total_memory / 1024**3, 2),
                }
                try:
                    device["allocated_gb"] = round(
                        torch.cuda.memory_allocated(i) / 1024**3, 2
                    )
                    device["reserved_gb"] = round(
                        torch.cuda.memory_reserved(i) / 1024**3, 2
                    )
                except Exception:  # environnement CUDA partiel -> champs omis
                    pass
                info["devices"].append(device)

    # 2) Vue pilote (nvidia-smi) -----------------------------------------------
    smi_stats = {}
    try:
        smi_stats = _query_nvidia_smi()
    except Exception:
        smi_stats = {}
    if smi_stats:
        info["source"].append("nvidia-smi")

    for index, stats in smi_stats.items():
        match = next((d for d in info["devices"] if d.get("index") == index), None)
        if match is None:
            match = {"index": index, "name": stats["smi_name"]}
            info["devices"].append(match)
            if stats["memory_total_mb"]:
                match["total_memory_gb"] = round(stats["memory_total_mb"] / 1024, 2)
            if not info["cuda_available"]:
                info["cuda_available"] = True
        match.update(
            {
                "utilization_percent": stats["utilization_percent"],
                "memory_used_mb": stats["memory_used_mb"],
                "memory_total_mb": stats["memory_total_mb"],
            }
        )

    info["device_count"] = len(info["devices"])
    if not info["devices"]:
        info["message"] = (
            "Aucun GPU détecté : ni CUDA/torch disponible, ni nvidia-smi exploitable."
        )
    return info