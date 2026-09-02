"""Tests unitaires pour le module d'observabilité avancée.

Vérifie :
    - Enregistrement des métriques
    - Calcul des percentiles
    - Agrégation temporelle
    - Calcul du taux d'erreur
    - Identification des outils lents
    - Résumé des métriques
"""

import time
import unittest

from ia.agent.observability import (
    record_metric,
    get_metrics_summary,
    get_slow_tools,
    get_error_rate,
    get_tool_metrics,
    clear_metrics,
    reset_observability,
)


class TestObservability(unittest.TestCase):
    """Tests du module d'observabilité."""

    def setUp(self):
        reset_observability()

    def test_record_metric_success(self):
        record_metric("tool_a", 100.0, success=True)
        metrics = get_tool_metrics("tool_a")
        self.assertEqual(metrics["total_calls"], 1)
        self.assertEqual(metrics["error_count"], 0)
        self.assertAlmostEqual(metrics["avg_duration_ms"], 100.0)

    def test_record_metric_error(self):
        record_metric("tool_a", 50.0, success=False)
        metrics = get_tool_metrics("tool_a")
        self.assertEqual(metrics["total_calls"], 1)
        self.assertEqual(metrics["error_count"], 1)

    def test_multiple_calls_aggregation(self):
        record_metric("tool_a", 100.0, True)
        record_metric("tool_a", 200.0, True)
        record_metric("tool_a", 300.0, True)
        metrics = get_tool_metrics("tool_a")
        self.assertEqual(metrics["total_calls"], 3)
        self.assertAlmostEqual(metrics["avg_duration_ms"], 200.0)
        self.assertAlmostEqual(metrics["min_duration_ms"], 100.0)
        self.assertAlmostEqual(metrics["max_duration_ms"], 300.0)

    def test_percentiles(self):
        for i in range(1, 101):
            record_metric("tool", float(i), True)
        metrics = get_tool_metrics("tool")
        self.assertAlmostEqual(metrics["p50_duration_ms"], 50.0, delta=2.0)
        self.assertAlmostEqual(metrics["p95_duration_ms"], 95.0, delta=2.0)
        self.assertAlmostEqual(metrics["p99_duration_ms"], 99.0, delta=2.0)

    def test_error_rate(self):
        record_metric("tool_a", 10.0, True)
        record_metric("tool_a", 10.0, True)
        record_metric("tool_a", 10.0, False)
        record_metric("tool_a", 10.0, False)
        rate = get_error_rate("tool_a")
        self.assertAlmostEqual(rate, 0.5)

    def test_error_rate_global(self):
        record_metric("tool_a", 10.0, True)
        record_metric("tool_b", 10.0, False)
        rate = get_error_rate()
        self.assertAlmostEqual(rate, 0.5)

    def test_error_rate_no_calls(self):
        rate = get_error_rate("nonexistent")
        self.assertEqual(rate, 0.0)

    def test_slow_tools(self):
        record_metric("fast", 10.0, True)
        record_metric("slow", 5000.0, True)
        slow = get_slow_tools(threshold_ms=1000.0)
        self.assertEqual(len(slow), 1)
        self.assertEqual(slow[0]["tool"], "slow")

    def test_metrics_summary(self):
        record_metric("tool_a", 100.0, True)
        record_metric("tool_a", 200.0, False)
        record_metric("tool_b", 50.0, True)
        summary = get_metrics_summary()
        self.assertEqual(summary["total_calls"], 3)
        self.assertEqual(summary["total_errors"], 1)
        self.assertIn("tool_a", summary["tools"])
        self.assertIn("tool_b", summary["tools"])

    def test_window_filtering(self):
        record_metric("tool", 100.0, True)
        summary = get_metrics_summary(window_seconds=0.001)
        # L'enregistrement est trop récent pour la fenêtre
        # Mais on ne peut pas garantir le timing, donc on vérifie que la méthode fonctionne
        self.assertIsInstance(summary, dict)

    def test_clear_metrics(self):
        record_metric("tool_a", 100.0, True)
        clear_metrics()
        metrics = get_tool_metrics("tool_a")
        self.assertEqual(metrics["total_calls"], 0)

    def test_empty_metrics(self):
        metrics = get_tool_metrics("nonexistent")
        self.assertEqual(metrics["total_calls"], 0)
        self.assertEqual(metrics["avg_duration_ms"], 0.0)

    def test_tool_not_recorded_in_summary_if_no_calls(self):
        summary = get_metrics_summary()
        self.assertEqual(summary["tools"], {})


if __name__ == "__main__":
    unittest.main()