# project/api/routes/metrics.py

import time

from fastapi import APIRouter
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, generate_latest

router = APIRouter(tags=["Metrics"])


@router.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@router.get("/metrics/json")
def metrics_json():
    """Snapshot des métriques Prometheus au format JSON.

    Endpoint de secours (« scraping via un proxy JSON ») pour le dashboard :
    quand le parse direct du format texte `/metrics` est bloqué (CORS) ou
    échoue, le front peut consommer la même information, déjà agrégée, depuis
    le registre prometheus_client.

    Retour :
      {
        "scrape_at_ms": 1710000000000,
        "counters":   [{"name": "http_requests_total", "labels": {...}, "value": 42}],
        "histograms":[{"name": "http_request_duration_seconds", "labels": {...}, "count": 4, "sum": 1.2}]
      }
    """
    scrape_at_ms = int(time.time() * 1000)
    counters = []

    for metric in REGISTRY.collect():
        name = metric.name
        for sample in metric.samples:
            labels = {k: v for k, v in sample.labels.items()}
            bucket_label = labels.pop("le", None)

            # Les échantillons d'un histogramme sont gérés par _collect_histograms.
            if bucket_label is not None or sample.name.endswith(("_sum", "_count")):
                continue
            # Compteur simple ou gauge (sample.name == metric.name).
            counters.append({"name": sample.name, "labels": labels, "value": sample.value})

    # Histogrammes, rassemblés par entrée {name, labels, count, sum, buckets}.
    return Response(
        _json_dumps(
            {
                "scrape_at_ms": scrape_at_ms,
                "counters": counters,
                "histograms": _collect_histograms(REGISTRY),
            }
        ),
        media_type="application/json",
    )


def _collect_histograms(registry=REGISTRY):
    """Reconstruit une représentation lisible des histogrammes du registre.

    Pour chaque sample `_sum` on crée une entrée {name, labels, count, sum},
    dans laquelle `count` est rempli par le sample `_count` (+Inf) correspondant
    et `buckets` par les tranches intermédiaires (le=...). Les samples du
    registre sont déjà groupés par métrique, on n'a donc pas à les réordonner.
    """
    # Première passe : indexer les samples par (name, clé de labels).
    sums = {}
    counts = {}
    buckets = {}

    def label_key(labels):
        return tuple(sorted(labels.items()))

    for metric in registry.collect():
        name = metric.name
        for sample in metric.samples:
            labels = {k: v for k, v in sample.labels.items()}
            bucket_label = labels.pop("le", None)
            key = label_key(labels)

            if sample.name.endswith("_sum"):
                sums.setdefault((name, key), {"name": name, "labels": labels, "count": 0, "sum": sample.value, "buckets": []})
            elif bucket_label == "+Inf":
                counts[(name, key)] = sample.value
            elif bucket_label is not None:
                buckets.setdefault((name, key), []).append(
                    {"bucket": bucket_label, "count": int(sample.value)}
                )

    out = []
    for (name, key), entry in sums.items():
        out.append(
            {
                "name": name,
                "labels": entry["labels"],
                "count": int(counts.get((name, key), 0)),
                "sum": entry["sum"],
                "buckets": sorted(
                    buckets.get((name, key), []),
                    key=lambda b: float(b["bucket"]),
                ),
            }
        )
    return out


def _json_dumps(payload):
    import json

    return json.dumps(payload, separators=(",", ":"))
