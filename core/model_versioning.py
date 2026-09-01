# project/core/model_versioning.py

import os
import json
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

MODEL_ROOT = os.path.join("experiments", "models")
MODELS_ROOT = MODEL_ROOT
MODEL_FILES = ["model.pt", "pytorch_model.bin", "model.safetensors", "model_state_dict.pt"]


def list_model_versions() -> list[str]:
    os.makedirs(MODEL_ROOT, exist_ok=True)
    logger.debug(f"list_model_versions : début | racine={MODEL_ROOT}")
    versions = []

    for name in os.listdir(MODEL_ROOT):
        path = os.path.join(MODEL_ROOT, name)
        if os.path.isdir(path):
            if any(
                os.path.isfile(os.path.join(path, f))
                and os.path.getsize(os.path.join(path, f)) > 0
                for f in MODEL_FILES
            ):
                versions.append(name)

    versions.sort(reverse=True)
    logger.debug(f"list_model_versions : terminé | {len(versions)} version(s) -> {versions}")
    return versions


def resolve_model_dir(model_name: str | None = None) -> str:
    logger.debug(f"resolve_model_dir : début | model_name={model_name}")
    if model_name:
        candidate = os.path.join(MODEL_ROOT, model_name)
        if not os.path.isdir(candidate):
            raise RuntimeError(f"Model version '{model_name}' not found.")
        logger.debug(f"resolve_model_dir : terminé -> {candidate}")
        return candidate

    versions = list_model_versions()
    if not versions:
        raise RuntimeError(
            "No valid model versions found in "
            f"{os.path.abspath(MODEL_ROOT)}. Train a model first (POST /train)."
        )

    # Pointeur de version active (SCRUM-55) : prioritaire sur la derniere version.
    # Import paresseux pour eviter une dependance circulaire (model_activation
    # importe MODEL_ROOT / list_model_versions depuis ce module).
    try:
        from core.model_activation import get_active_model_dir

        active_dir = get_active_model_dir()
        if active_dir and os.path.isdir(active_dir):
            logger.debug(f"resolve_model_dir : version active -> {active_dir}")
            return active_dir
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Pointeur actif indisponible (%s) ; repli sur la derniere version.", exc)

    selected = os.path.join(MODEL_ROOT, versions[0])
    logger.debug(f"resolve_model_dir : dernière version -> {selected}")
    return selected


def resolve_model_path(model_arg: str | None = None) -> str:
    """
    Résout l'argument modèle passé en ligne de commande :
      - None             -> dernière version valide dans MODEL_ROOT ;
      - nom de version   -> MODEL_ROOT/<version>  (ex: "20260819T151459Z") ;
      - dossier existant -> utilisé tel quel (chemin absolu ou relatif).

    Tolérant : si l'argument n'est ni un dossier ni une version connue, il est
    renvoyé tel quel (avec un avertissement listant les versions disponibles) —
    Predictor lèvera alors sa propre erreur si le chemin est invalide. Seule
    l'absence totale de modèle (model_arg=None) lève FileNotFoundError.
    """
    logger.debug(f"resolve_model_path : début | model_arg={model_arg}")
    if model_arg:
        # Chemin de dossier explicite (absolu ou relatif) -> tel quel.
        if os.path.isdir(model_arg):
            logger.debug(f"resolve_model_path : terminé (dossier explicite) -> {model_arg}")
            return model_arg
        try:
            resolved = resolve_model_dir(model_arg)
            logger.debug(f"resolve_model_path : terminé -> {resolved}")
            return resolved
        except RuntimeError:
            available = ", ".join(list_model_versions()[:5])
            logger.warning(
                f"[warn] '{model_arg}' n'est ni un dossier existant ni une version "
                f"de {MODEL_ROOT} (versions disponibles : {available or 'aucune'}). "
                "Utilisé tel quel."
            )
            logger.debug(f"resolve_model_path : terminé (argument toléré) -> {model_arg}")
            return model_arg

    try:
        resolved = resolve_model_dir()
        logger.debug(f"resolve_model_path : terminé -> {resolved}")
        return resolved
    except RuntimeError as exc:
        raise FileNotFoundError(f"Aucun modèle disponible : {exc}")


def _json_safe(value):
    """Convertit des valeurs non sérialisables (tensor, numpy, MagicMock…) en types JSON natifs."""
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if hasattr(value, "item"):  # torch.Tensor / numpy scalar
        return _json_safe(value.item())
    return str(value)


def _validate_saved_model(model_dir):
    """Vérifie qu'un modèle sauvegardé est exploitable.

    Lève ``RuntimeError`` si :
      - aucun fichier de poids non vide n'est présent ;
      - la tête de classification est quasi-aléatoire (jamais entraînée).
    """
    # 1. Fichier de poids non vide présent ?
    weight_file = None
    for fname in MODEL_FILES:
        fpath = os.path.join(model_dir, fname)
        if os.path.isfile(fpath) and os.path.getsize(fpath) > 0:
            weight_file = fpath
            break
    if weight_file is None:
        raise RuntimeError(
            f"Échec de sauvegarde : aucun fichier de poids non vide dans {model_dir}. "
            f"Attendu l'un de : {MODEL_FILES}"
        )

    # 2. Tête de classification réellement entraînée ?
    from core.model_head_check import is_model_version_trained

    if not is_model_version_trained(model_dir):
        raise RuntimeError(
            f"Échec de sauvegarde : la tête de classification de {model_dir} apparaît "
            "quasi-aléatoire (probablement jamais entraînée). "
            "Vérifie que le modèle entraîné est bien celui persisté."
        )


def _save_trained_model(trainer, model_dir):
    """Persiste les poids du modèle entraîné dans le dossier versionné.

    Stratégie atomique + validée :
      1. Sauvegarde dans un dossier temporaire ``<model_dir>.tmp`` ;
      2. Validation (poids non vide + tête entraînée) ;
                  3. Fusion des poids validés dans ``<model_dir>`` (préserve le tokenizer) ;
      4. Nettoyage du temporaire en cas d'échec.

    Lève ``RuntimeError`` si la validation échoue — un modèle inexploitable
    n'est JAMAIS publié dans ``experiments/models``.
    """
    import shutil

    tmp_dir = model_dir + ".tmp"
    # Nettoyage d'un éventuel temporaire résiduel.
    if os.path.isdir(tmp_dir):
        shutil.rmtree(tmp_dir)

    # --- 1. Sauvegarde dans le temporaire -----------------------------------
    save_fn = getattr(trainer, "save", None)
    if callable(save_fn):
        save_fn(tmp_dir)
    else:
        model = getattr(trainer, "model", None)
        if model is None:
            if os.path.isdir(tmp_dir):
                shutil.rmtree(tmp_dir)
            return

        if hasattr(model, "save_pretrained"):
            model.save_pretrained(tmp_dir)
        else:
            import torch

            os.makedirs(tmp_dir, exist_ok=True)
            torch.save(model.state_dict(), os.path.join(tmp_dir, "model_state_dict.pt"))

    try:
        # --- 2. Validation (sur le temporaire, AVANT publication) -------------
        _validate_saved_model(tmp_dir)

        # --- 3. Publication des poids dans model_dir (fusion, pas écrasement) --
        # model_dir peut déjà contenir les fichiers du tokenizer (vocab.json,
        # tokenizer_config.json, special_tokens_map.json, …) écrits par
        # save_model_version() via tokenizer.save_pretrained(model_dir) avant
        # cet appel. Un remplacement par ``shutil.rmtree(model_dir)`` +
        # ``os.rename(tmp_dir, model_dir)`` détruirait ces fichiers, laissant
        # le dossier avec des poids mais sans tokenizer — /predict échouerait
        # ensuite avec ``FileNotFoundError: vocab.json`` (et de même
        # AutoTokenizer.from_pretrained échouerait en production).
        #
        # On propage donc les poids validés par fusion dans model_dir (en
        # écrasant d'éventuelles versions précédentes des mêmes fichiers),
        # puis on retire le temporaire.
        os.makedirs(model_dir, exist_ok=True)
        for entry in os.listdir(tmp_dir):
            src = os.path.join(tmp_dir, entry)
            dst = os.path.join(model_dir, entry)
            if os.path.isdir(src):
                if os.path.isdir(dst):
                    shutil.rmtree(dst)
                shutil.move(src, dst)
            else:
                shutil.copy2(src, dst)
    except Exception:
        # --- 4. Nettoyage sur échec -------------------------------------------
        if os.path.isdir(tmp_dir):
            shutil.rmtree(tmp_dir)
        raise

    # --- 5. Nettoyage du temporaire après publication réussie -----------------
    if os.path.isdir(tmp_dir):
        shutil.rmtree(tmp_dir)


def write_label_mappings(trainer, model_dir):
    """Ecrit id2label.json / label2id.json dans le dossier versionne (SCRUM-55).

    Sources prioritaires : la config du trainer, sinon le model config HF.
    """
    labels = None
    cfg = getattr(trainer, "cfg", None) or {}
    if isinstance(cfg, dict) and isinstance(cfg.get("id2label"), dict):
        labels = {int(k): str(v) for k, v in cfg["id2label"].items()}
    if not labels:
        model = getattr(trainer, "model", None)
        hf_cfg = getattr(model, "config", None)
        id2label = getattr(hf_cfg, "id2label", None) if hf_cfg is not None else None
        if id2label:
            labels = {int(k): str(v) for k, v in id2label.items()}
    if not labels:
        logger.warning("write_label_mappings : aucun mapping de labels disponible.")
        return False
    if all(isinstance(k, int) for k in labels.keys()):
        id2label = {int(k): str(v) for k, v in labels.items()}
    else:
        # mapping nom -> int fourni
        id2label = {int(v): str(k) for k, v in labels.items()}
    label2id = {v: k for k, v in id2label.items()}
    with open(os.path.join(model_dir, "id2label.json"), "w", encoding="utf-8") as fh:
        json.dump({str(k): v for k, v in id2label.items()}, fh, indent=2, ensure_ascii=False)
    with open(os.path.join(model_dir, "label2id.json"), "w", encoding="utf-8") as fh:
        json.dump(label2id, fh, indent=2, ensure_ascii=False)
    logger.info("Mappings de labels ecrits dans %s", model_dir)
    return True


def _ensure_state_dict_pt(trainer, model_dir):
    """Persiste model_state_dict.pt si aucun fichier de poids lisible n'existe deja."""
    if any(
        os.path.isfile(os.path.join(model_dir, f)) and os.path.getsize(os.path.join(model_dir, f)) > 0
        for f in MODEL_FILES
    ):
        return
    model = getattr(trainer, "model", None)
    if model is None:
        return
    import torch

    torch.save(model.state_dict(), os.path.join(model_dir, "model_state_dict.pt"))
    logger.info("model_state_dict.pt ecrit dans %s", model_dir)


def validate_model_version(version_dir: str) -> dict:
    """Valide les artefacts d'une version (SCRUM-55).

    Verifications :
      - config.json present et parsable ;
      - un fichier de poids non vide (model.safetensors / model_state_dict.pt / ...) ;
      - id2label.json / label2id.json presents et coherents ;
      - tete de classification entrainee (ecart-type > 0.03).

    Leve ``ValueError`` si une verification echoue ; retourne un resume sinon.
    """
    version_dir = os.path.abspath(version_dir)
    errors = []
    if not os.path.isdir(version_dir):
        raise ValueError(f"Dossier de version inexistant : {version_dir}.")

    config_path = os.path.join(version_dir, "config.json")
    if not os.path.isfile(config_path):
        errors.append("config.json absent")
    else:
        try:
            with open(config_path, "r", encoding="utf-8") as fh:
                json.load(fh)
        except Exception as exc:
            errors.append(f"config.json illisible : {exc}")

    if not any(
        os.path.isfile(os.path.join(version_dir, f)) and os.path.getsize(os.path.join(version_dir, f)) > 0
        for f in MODEL_FILES
    ):
        errors.append("aucun fichier de poids non vide")

    id2label_path = os.path.join(version_dir, "id2label.json")
    label2id_path = os.path.join(version_dir, "label2id.json")
    if not (os.path.isfile(id2label_path) and os.path.isfile(label2id_path)):
        errors.append("id2label.json / label2id.json absents")
    else:
        try:
            with open(id2label_path, "r", encoding="utf-8") as fh:
                id2label = json.load(fh)
            with open(label2id_path, "r", encoding="utf-8") as fh:
                label2id = json.load(fh)
            if {v for v in id2label.values()} != set(label2id.keys()):
                errors.append("id2label / label2id incoherents")
        except Exception as exc:
            errors.append(f"mappings illisibles : {exc}")

    from core.model_head_check import is_model_version_trained

    if not is_model_version_trained(version_dir):
        errors.append("tete de classification non entrainee (ecart-type <= 0.03) ou poids illisibles")

    if errors:
        raise ValueError(
            f"Version invalide ({version_dir}) : " + "; ".join(errors) + "."
        )
    logger.info("Version validee : %s", version_dir)
    return {"version_dir": version_dir, "valid": True}


def save_model_version(tokenizer, trainer, job_id, train_examples, val_examples, started_at, finished_at):
    logger.info(
        f"Sauvegarde du modèle | job_id={job_id} | {train_examples} train / {val_examples} val"
    )
    os.makedirs(MODEL_ROOT, exist_ok=True)
    timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    logger.debug(f"Horodatage de la version : {timestamp}")
    model_dir = os.path.join(MODEL_ROOT, timestamp)
    os.makedirs(model_dir, exist_ok=True)

    tokenizer.save_pretrained(model_dir)

    # Poids + configuration du modèle entraîné dans le dossier versionné.
    # Indispensable : sans eux, list_model_versions() / resolve_model_dir()
    # et le Predictor (qui exige config.json ou un fichier de poids) ne
    # trouvent aucun modèle chargeable — POST /train produisait donc des
    # versions inutilisables par /predict.
    _save_trained_model(trainer, model_dir)

    # Artefacts SCRUM-55 : mappings id2label/label2id + state_dict de secours.
    write_label_mappings(trainer, model_dir)
    _ensure_state_dict_pt(trainer, model_dir)

    # Configuration d'entraînement (copie json-safe).
    hyperparameters = _json_safe(dict(getattr(trainer, "cfg", {}) or {}))

    # Métriques finales + séries temporelles par époque.
    epoch_metrics = getattr(trainer, "epoch_metrics", None) or []
    final_metrics = dict(getattr(trainer, "final_metrics", None) or {})
    metrics = _json_safe(
        {
            **final_metrics,
            "accuracy_by_epoch": [
                entry.get("accuracy") for entry in epoch_metrics
            ],
            "f1_by_epoch": [entry.get("f1_macro") for entry in epoch_metrics],
            "epochs": len(epoch_metrics),
        }
    )

    report = {
        "timestamp": timestamp,
        "job_id": job_id,
        "model_dir": model_dir,
        "hyperparameters": hyperparameters,
        "metrics": metrics,
        "training_duration_seconds": float(finished_at - started_at),
        "train_examples": train_examples,
        "val_examples": val_examples,
    }

    with open(os.path.join(model_dir, "training_report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, default=str)

    logger.info(f"Version du modèle enregistrée -> {model_dir}")
    return model_dir
