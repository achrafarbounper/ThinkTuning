"""
Calibration du modèle de sentiment multilingue : reliability diagram + ECE.

Vérifie que la confiance annoncée par le modèle reflète sa précision réelle.
Le script génère une calibration curve (aussi appelée reliability diagram)
construite avec ``sklearn.calibration.calibration_curve``, calcule l'ECE
(Expected Calibration Error) et enregistre un avertissement dans les logs si
l'ECE dépasse 0.1 (mauvais calibrage).

Pour un problème multiclasse, on utilise la calibration « top-label » :
    - y_true binaire : « la prédiction était-elle correcte ? » (1 ou 0)
    - y_prob         : la confidence du modèle sur la classe prédite
Un modèle parfaitement calibré suit alors la diagonale : quand il annonce
une confiance de 0.8, il a raison environ 8 fois sur 10.

Le temperature scaling post-hoc (Guo et al., 2017) peut être appliqué en
option : on divise les logits par une température T (T > 1 adoucit les
probabilités, T < 1 les rend plus tranchées). Cela ne change JAMAIS la classe
prédite (l'argmax est invariant), seulement la confiance associée.

Usage :
    python calibrate.py                              # dernier modèle de experiments/models
    python calibrate.py --model_name 20260825T153054Z  # une version précise
    python calibrate.py --temperature_scaling        # + T appris (minimisation NLL)
    python calibrate.py --temperature 1.8            # + application d'un T fixé

Le modèle calibré est choisi parmi les versions produites par train.py dans
``experiments/models`` (dossiers horodatés contenant config.json + poids) :
    - ``--model_name <version>`` : utilise explicitement cette version (une
      erreur claire est levée si le nom est absent ou invalide) ;
    - par défaut : la DERNIÈRE version valide (horodatage le plus récent).

Artefacts générés dans le dossier de sortie (outputs/ par défaut) :
    - calibration_curve.png        : reliability diagram avant ajustement
    - calibration_curve_after.png  : après temperature scaling (si activé)
    - calibration_report.json      : ECE, détail des bins, température retenue
"""

import argparse
import json
import logging
import os

import matplotlib
matplotlib.use("Agg")  # backend non interactif : fonctionne même sans affichage graphique
import matplotlib.pyplot as plt
import numpy as np
import torch
from sklearn.calibration import calibration_curve
from tqdm import tqdm

# On réutilise OUTPUT_DIR du module evaluate (accès dynamique via l'attribut
# du module pour rester monkeypatchable dans les tests).
import evaluate

# Modèles versionnés produits par train.py (voir core/model_versioning.py) :
# MODEL_ROOT = experiments/models, list_model_versions() filtre les versions
# VALIDES (présence de poids) triées de la plus récente à la plus ancienne.
from core.model_versioning import MODEL_ROOT, list_model_versions
from src.dataset.loader import load_raw_dataset
from src.dataset.preprocess import tokenize_dataset
from src.utils.config import load_config
from transformers import AutoTokenizer, AutoModelForSequenceClassification, DataCollatorWithPadding

logger = logging.getLogger(__name__)

# Au-delà de ce seuil d'ECE, on considère que la confiance du modèle ne
# reflète plus fidèlement sa précision réelle (mauvais calibrage).
ECE_WARNING_THRESHOLD = 0.1


def select_model_path(model_name=None):
    """
    Résout le chemin du modèle à calibrer parmi les versions de
    core.model_versioning.MODEL_ROOT (experiments/models) :

        - model_name fourni  -> doit figurer parmi les versions VALIDES
          (contenant des poids : model.safetensors, pytorch_model.bin ou
          model.pt), sinon ValueError avec la liste des versions disponibles ;
        - model_name omis    -> la DERNIÈRE version valide (les noms étant des
          horodatages ISO, le tri décroissant place la plus récente en tête).

    Retourne un tuple (model_path, model_label).
    """
    versions = list_model_versions()

    if model_name:
        if model_name not in versions:
            available = ", ".join(versions[:10])
            raise ValueError(
                f"Modèle '{model_name}' introuvable ou invalide dans {MODEL_ROOT} "
                f"(versions valides disponibles : {available or 'aucune'})."
            )
        return os.path.join(MODEL_ROOT, model_name), model_name

    if not versions:
        raise FileNotFoundError(
            f"Aucun modèle valide trouvé dans {MODEL_ROOT} : lancez d'abord un "
            "entraînement (python train.py) ou passez --model_name."
        )

    latest = versions[0]
    logger.info(
        "%d modèles valides trouvés dans %s ; utilisation du plus récent : %s",
        len(versions), MODEL_ROOT, latest,
    )
    return os.path.join(MODEL_ROOT, latest), latest


def collect_logits(model, tokenizer, dataset, batch_size=16):
    """
    Passe le modèle sur un dataset HuggingFace tokenisé et collecte les logits
    bruts (mécanique identique à evaluate.evaluate : padding dynamique par
    batch via DataCollatorWithPadding).

    Retourne un tuple (logits, labels) :
        logits : array numpy (n_samples, n_classes)
        labels : array numpy (n_samples,)
    """
    if "label" in dataset.column_names and "labels" not in dataset.column_names:
        dataset = dataset.rename_column("label", "labels")

    label_key = "labels" if "labels" in dataset.column_names else "label"
    dataset.set_format(
        type="torch",
        columns=["input_ids", "attention_mask", label_key],
    )

    collator = DataCollatorWithPadding(tokenizer=tokenizer)
    loader = torch.utils.data.DataLoader(
        dataset,
        batch_size=batch_size,
        shuffle=False,
        collate_fn=collator,
    )

    all_logits, all_labels = [], []
    model.eval()
    with torch.no_grad():
        for batch in tqdm(loader, desc="Calibration"):
            input_ids = batch["input_ids"]
            attention_mask = batch["attention_mask"]
            label = batch.get("labels", batch.get(label_key))

            logits = model(
                input_ids=input_ids,
                attention_mask=attention_mask,
            ).logits

            all_logits.append(logits.cpu().numpy())
            all_labels.append(label.cpu().numpy())

    return np.concatenate(all_logits, axis=0), np.concatenate(all_labels, axis=0)


def apply_temperature_scaling(logits, temperature):
    """
    Applique le temperature scaling post-hoc : softmax(logits / T).

    T > 1 adoucit la distribution (baisse la confiance), T < 1 la rend plus
    tranchée. L'argmax est invariant : les prédictions restent identiques.

    Retourne un array numpy (n_samples, n_classes) de probabilités sommant à 1.
    """
    t = float(temperature)
    if t <= 0:
        raise ValueError(f"La température doit être strictement positive, reçu : {t}")

    scaled = np.asarray(logits, dtype=np.float64) / t
    # Décalage par le max pour la stabilité numérique de l'exponentielle.
    shifted = scaled - scaled.max(axis=-1, keepdims=True)
    exp = np.exp(shifted)
    return exp / exp.sum(axis=-1, keepdims=True)


def fit_temperature(logits, labels, max_iter=200):
    """
    Apprend la température T qui minimise la negative log-likelihood sur
    (logits, labels) — Guo et al., 2017 « On Calibration of Modern Neural
    Networks ». Optimisation LBFGS via torch, avec paramétrisation log(T)
    garantissant T > 0.

    Remarque : idéalement, T doit être ajusté sur un jeu de VALIDATION
    distinct du jeu d'évaluation final pour ne pas biaiser le rapport.

    Retourne un float T (> 0). En cas de données dégénérées (moins de deux
    classes présentes, optimisation non convergeante), retourne 1.0.
    """
    logits_np = np.asarray(logits, dtype=np.float32)
    logits_t = torch.as_tensor(logits_np)
    # Les labels peuvent être des arrays numpy ou des tensors torch : on gère
    # les deux (np.asarray n'accepte pas un dtype torch comme paramètre).
    if isinstance(labels, torch.Tensor):
        labels_t = labels.detach().reshape(-1).to(dtype=torch.long)
    else:
        labels_t = torch.as_tensor(np.asarray(labels).reshape(-1),
                                   dtype=torch.long)

    if logits_t.numel() == 0 or labels_t.unique().numel() < 2:
        logger.warning(
            "Temperature scaling impossible (données dégénérées : %d exemples, "
            "%d classes présentes) : T=1.0 conservé.",
            labels_t.numel(), labels_t.unique().numel(),
        )
        return 1.0

    log_t = torch.zeros(1, requires_grad=True)
    optimizer = torch.optim.LBFGS([log_t], lr=0.05, max_iter=max_iter)

    def closure():
        optimizer.zero_grad()
        loss = torch.nn.functional.cross_entropy(logits_t / log_t.exp(), labels_t)
        loss.backward()
        return loss

    try:
        optimizer.step(closure)
    except RuntimeError as exc:
        logger.warning("Optimisation LBFGS échouée (%s) : T=1.0 conservé.", exc)
        return 1.0

    if not torch.isfinite(log_t).all():
        logger.warning("Optimisation non convergeante (T non fini) : T=1.0 conservé.")
        return 1.0

    temperature = float(log_t.exp().item())
    # Garde-fou numérique contre des températures extrêmes.
    return float(np.clip(temperature, 1e-2, 1e2))


def expected_calibration_error(confidences, correct, n_bins=10):
    """
    Calcule l'ECE (Expected Calibration Error) :

        ECE = somme_b (n_b / N) * | précision_réelle_b - confiance_moyenne_b |

    avec des bins équi-larges sur [0, 1] (le dernier bin inclut la valeur 1.0).
    Chaque bin est pondéré par son nombre d'exemples, contrairement à une
    simple moyenne des gaps.

    Retourne un tuple (ece, bin_details) où bin_details est la liste des bins
    non vides : {bin_low, bin_high, count, mean_confidence, accuracy, gap}.
    """
    confidences = np.asarray(confidences, dtype=float).reshape(-1)
    correct = np.asarray(correct, dtype=float).reshape(-1)
    n = confidences.size
    if n == 0:
        return 0.0, []

    # Bin équi-large k = floor(c * n_bins) ; clip pour placer 1.0 dans le
    # dernier bin.
    bin_ids = np.clip((confidences * n_bins).astype(int), 0, n_bins - 1)
    edges = np.linspace(0.0, 1.0, n_bins + 1)

    ece = 0.0
    details = []
    for b in range(n_bins):
        mask = bin_ids == b
        count = int(mask.sum())
        if count == 0:
            continue
        mean_confidence = float(confidences[mask].mean())
        accuracy = float(correct[mask].mean())
        gap = abs(accuracy - mean_confidence)
        ece += (count / n) * gap
        details.append({
            "bin_low": float(edges[b]),
            "bin_high": float(edges[b + 1]),
            "count": count,
            "mean_confidence": mean_confidence,
            "accuracy": accuracy,
            "gap": float(gap),
        })
    return float(ece), details


def reliability_curve(confidences, correct, n_bins=10, strategy="uniform"):
    """
    Construit la courbe de calibration (reliability diagram) avec
    sklearn.calibration.calibration_curve :
        - y_true binaire = « la prédiction était correcte »
        - y_prob         = confidence du modèle sur la classe prédite

    Retourne (prob_true, prob_pred) : précision réelle et confiance moyenne
    par bin (bins vides retirés par sklearn).
    """
    prob_true, prob_pred = calibration_curve(
        np.asarray(correct, dtype=int).reshape(-1),
        np.asarray(confidences, dtype=float).reshape(-1),
        n_bins=n_bins,
        strategy=strategy,
    )
    return prob_true, prob_pred


def plot_reliability_diagram(prob_true, prob_pred, ece, save_path,
                             title="Diagramme de fiabilité", temperature=None):
    """
    Trace et sauvegarde le reliability diagram : précision réelle en fonction
    de la confiance prédite, avec la diagonale « parfaitement calibré » en
    référence et l'ECE en annotation.

    Retourne le chemin de la figure sauvegardée.
    """
    fig, ax = plt.subplots(figsize=(6.4, 6.0))

    ax.plot([0, 1], [0, 1], linestyle="--", color="gray", linewidth=1,
            label="Parfaitement calibré")
    ax.plot(prob_pred, prob_true, marker="o", color="tab:blue", label="Modèle")

    suffix = f" (T = {temperature:.3f})" if temperature is not None else ""
    ax.set_title(f"{title}{suffix}\nECE = {ece:.4f}")
    ax.set_xlabel("Confidence prédite (moyenne par bin)")
    ax.set_ylabel("Précision réelle (fraction correcte)")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.grid(alpha=0.3)
    ax.legend(loc="best")

    os.makedirs(os.path.dirname(save_path) or ".", exist_ok=True)
    fig.savefig(save_path, bbox_inches="tight")
    plt.close(fig)
    return save_path


def warn_if_miscalibrated(ece, threshold=ECE_WARNING_THRESHOLD):
    """
    Enregistre un avertissement dans les logs si l'ECE dépasse le seuil
    (0.1 par défaut) afin de détecter rapidement un mauvais calibrage.

    Retourne True si un avertissement a été émis, sinon False.
    """
    if ece > threshold:
        logger.warning(
            "MAUVAIS CALIBRAGE : ECE=%.4f > seuil=%.2f — la confidence ne reflète "
            "pas la précision réelle. Envisager un temperature scaling post-hoc "
            "(python calibrate.py --temperature_scaling).",
            ece, threshold,
        )
        return True
    logger.info("Calibration satisfaisante : ECE=%.4f <= seuil=%.2f.", ece, threshold)
    return False


def analyze(logits, labels, n_bins=10, strategy="uniform", temperature=1.0):
    """
    Analyse complète de la calibration pour des logits donnés, après
    application optionnelle d'une température.

    Retourne un dict : probs, confidences, correct, accuracy, mean_confidence,
    ece, bins, prob_true, prob_pred, temperature.
    """
    probs = apply_temperature_scaling(logits, temperature)
    confidences = probs.max(axis=1)
    preds = probs.argmax(axis=1)
    correct = (preds == np.asarray(labels)).astype(float)

    accuracy = float(correct.mean())
    ece, bins = expected_calibration_error(confidences, correct, n_bins=n_bins)
    prob_true, prob_pred = reliability_curve(
        confidences, correct, n_bins=n_bins, strategy=strategy,
    )

    return {
        "probs": probs,
        "confidences": confidences,
        "correct": correct,
        "accuracy": accuracy,
        "mean_confidence": float(confidences.mean()),
        "ece": ece,
        "bins": bins,
        "prob_true": prob_true,
        "prob_pred": prob_pred,
        "temperature": float(temperature),
    }


def _summary_block(result):
    """Bloc sérialisable en JSON décrivant le résultat d'une analyse."""
    return {
        "temperature": result["temperature"],
        "accuracy": result["accuracy"],
        "mean_confidence": result["mean_confidence"],
        "ece": result["ece"],
        "bins": result["bins"],
    }


def main(args):
    # Les avertissements de calibration doivent apparaître dans les logs
    # console du script (format horodaté standard).
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    output_dir = args.output_dir or evaluate.OUTPUT_DIR

    # Même max_length qu'à l'entraînement (cf. commentaire de evaluate.main) :
    # sinon le modèle voit des séquences différentes et l'ECE est faussé.
    cfg = load_config("configs/default.yaml")
    max_length = args.max_length or cfg["max_length"]

    # Choix du modèle : version explicite (--model_name) ou dernière version
    # valide de experiments/models. Résolu AVANT le chargement du dataset pour
    # échouer immédiatement si la version demandée est invalide.
    model_path, model_label = select_model_path(args.model_name)

    print(f"1. Chargement du dataset FR/EN (max {args.max_per_lang}/langue)...")
    raw = load_raw_dataset(max_per_lang=args.max_per_lang)

    print(f"2. Chargement du tokenizer et du modèle ({model_label})...")
    tokenizer = AutoTokenizer.from_pretrained(model_path)
    model = AutoModelForSequenceClassification.from_pretrained(model_path)

    print(f"3. Tokenisation du dataset (max_length={max_length})...")
    tokenized = tokenize_dataset(raw, tokenizer, max_length=max_length)

    print("4. Collecte des logits...")
    logits, labels = collect_logits(model, tokenizer, tokenized,
                                    batch_size=args.batch_size)

    print("5. Analyse de la calibration (avant ajustement)...")
    base = analyze(logits, labels, n_bins=args.n_bins, strategy=args.strategy,
                   temperature=1.0)
    print("\n=== Calibration initiale (T=1.0) ===")
    print(f"Accuracy         : {base['accuracy']:.4f}")
    print(f"Confiance moyenne: {base['mean_confidence']:.4f}")
    print(f"ECE              : {base['ece']:.4f} (seuil d'alerte : "
          f"{ECE_WARNING_THRESHOLD})")

    # Critère d'acceptation : avertissement dans les logs si ECE > 0.1.
    warn_if_miscalibrated(base["ece"])

    plot_path = os.path.join(output_dir, "calibration_curve.png")
    plot_reliability_diagram(base["prob_true"], base["prob_pred"], base["ece"],
                             plot_path)
    print(f"[Calibration] Figure sauvegardée : {plot_path}")

    report = {
        "model_name": model_label,
        "model_path": model_path,
        "n_samples": int(labels.size),
        "n_bins": args.n_bins,
        "strategy": args.strategy,
        "ece_warning_threshold": ECE_WARNING_THRESHOLD,
        "before": _summary_block(base),
        "after": None,
    }

    # --- Temperature scaling post-hoc (optionnel) ------------------------- #
    temperature = None
    if args.temperature_scaling:
        print("\n6. Ajustement de la température (minimisation NLL, LBFGS)...")
        temperature = fit_temperature(logits, labels)
        print(f"Température apprise : T = {temperature:.4f}")
    elif args.temperature is not None:
        temperature = float(args.temperature)
        print(f"\n6. Application de la température imposée : T = {temperature:.4f}")

    if temperature is not None:
        adjusted = analyze(logits, labels, n_bins=args.n_bins,
                           strategy=args.strategy, temperature=temperature)
        print(f"\n=== Calibration après temperature scaling "
              f"(T={adjusted['temperature']:.4f}) ===")
        print(f"Accuracy         : {adjusted['accuracy']:.4f} (inchangée : "
              f"l'argmax est invariant)")
        print(f"Confiance moyenne: {adjusted['mean_confidence']:.4f}")
        print(f"ECE              : {adjusted['ece']:.4f} (avant : {base['ece']:.4f})")

        # Nouvelle vérification : le scaling suffit-il à repasser sous le seuil ?
        warn_if_miscalibrated(adjusted["ece"])

        after_path = os.path.join(output_dir, "calibration_curve_after.png")
        plot_reliability_diagram(adjusted["prob_true"], adjusted["prob_pred"],
                                 adjusted["ece"], after_path,
                                 title="Diagramme de fiabilité (après scaling)",
                                 temperature=adjusted["temperature"])
        print(f"[Calibration] Figure sauvegardée : {after_path}")

        report["after"] = _summary_block(adjusted)

    os.makedirs(output_dir, exist_ok=True)
    report_path = os.path.join(output_dir, "calibration_report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"[Calibration] Rapport sauvegardé : {report_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Courbe de calibration (reliability diagram) + ECE + "
                    "temperature scaling post-hoc optionnel."
    )
    parser.add_argument("--max_per_lang", type=int, default=500)
    parser.add_argument("--batch_size", type=int, default=16)
    parser.add_argument("--max_length", type=int, default=None,
                        help="Par défaut, réutilise max_length de configs/default.yaml")
    parser.add_argument("--model_name", type=str, default=None,
                        help="Version à calibrer dans experiments/models "
                             "(ex. 20260825T153054Z). Par défaut : la dernière "
                             "version valide.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--temperature_scaling", action="store_true",
                       help="Apprend T post-hoc (minimisation NLL) puis l'applique.")
    group.add_argument("--temperature", type=float, default=None,
                       help="Applique une température fixe fournie (ex. 1.8).")
    parser.add_argument("--n_bins", type=int, default=10,
                        help="Nombre de bins pour l'ECE et la courbe (défaut : 10).")
    parser.add_argument("--strategy", choices=["uniform", "quantile"],
                        default="uniform",
                        help="Stratégie de binning passée à sklearn.calibration_curve.")
    parser.add_argument("--output_dir", type=str, default=None,
                        help="Dossier des artefacts "
                             "(défaut : OUTPUT_DIR de evaluate.py, i.e. outputs/).")

    main(parser.parse_args())



