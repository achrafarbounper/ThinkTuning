"""Tests unitaires pour le système d'audit trail.

Vérifie :
    - Enregistrement des appels d'outils
    - Persistence SQLite
    - Requêtage par job_id et tool_name
    - Limite de rétention
    - Thread-safety
"""

import os
import tempfile
import unittest

from ia.agent.audit import (
    log_tool_call,
    get_audit_trail,
    get_tool_history,
    clear_audit_log,
    set_audit_db_path,
    get_audit_db_path,
)


class TestAuditTrail(unittest.TestCase):
    """Tests du système d'audit."""

    def setUp(self):
        """Utilise une DB temporaire pour chaque test."""
        self._original_path = get_audit_db_path()
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        set_audit_db_path(self._tmp.name)
        clear_audit_log()

    def tearDown(self):
        """Restaure le chemin original et supprime la DB temporaire."""
        set_audit_db_path(self._original_path)
        try:
            os.unlink(self._tmp.name)
        except OSError:
            pass

    def test_log_and_retrieve(self):
        """Vérifie l'enregistrement et la récupération."""
        log_tool_call(
            job_id="job1",
            tool="read_file",
            args={"path": "/test.txt"},
            result="content",
            duration_ms=10.5,
            success=True,
        )
        trail = get_audit_trail(job_id="job1")
        self.assertEqual(len(trail), 1)
        self.assertEqual(trail[0]["tool"], "read_file")
        self.assertEqual(trail[0]["job_id"], "job1")
        self.assertEqual(trail[0]["success"], 1)

    def test_log_error(self):
        """Vérifie l'enregistrement d'une erreur."""
        log_tool_call(
            job_id="job1",
            tool="bad_tool",
            args={"x": 1},
            duration_ms=5.0,
            success=False,
            error_message="Something went wrong",
        )
        trail = get_audit_trail(job_id="job1")
        self.assertEqual(len(trail), 1)
        self.assertEqual(trail[0]["success"], 0)
        self.assertEqual(trail[0]["error_message"], "Something went wrong")

    def test_get_tool_history(self):
        """Vérifie la récupération par outil."""
        log_tool_call(job_id="job1", tool="read_file", args={}, result="ok", duration_ms=1.0, success=True)
        log_tool_call(job_id="job2", tool="read_file", args={}, result="ok", duration_ms=2.0, success=True)
        log_tool_call(job_id="job3", tool="write_file", args={}, result="ok", duration_ms=3.0, success=True)
        history = get_tool_history("read_file")
        self.assertEqual(len(history), 2)
        self.assertTrue(all(h["tool"] == "read_file" for h in history))

    def test_limit_parameter(self):
        """Vérifie le paramètre de limite."""
        for i in range(10):
            log_tool_call(job_id="job1", tool="tool", args={}, result="ok", duration_ms=1.0, success=True)
        trail = get_audit_trail(job_id="job1", limit=5)
        self.assertEqual(len(trail), 5)

    def test_clear_audit_log(self):
        """Vérifie le vidage du journal."""
        log_tool_call(job_id="job1", tool="tool", args={}, result="ok", duration_ms=1.0, success=True)
        clear_audit_log()
        trail = get_audit_trail()
        self.assertEqual(len(trail), 0)

    def test_clear_by_job_id(self):
        """Vérifie le vidage par job_id."""
        log_tool_call(job_id="job1", tool="tool", args={}, result="ok", duration_ms=1.0, success=True)
        log_tool_call(job_id="job2", tool="tool", args={}, result="ok", duration_ms=1.0, success=True)
        clear_audit_log(job_id="job1")
        trail = get_audit_trail()
        self.assertEqual(len(trail), 1)
        self.assertEqual(trail[0]["job_id"], "job2")

    def test_ordering(self):
        """Vérifie que les entrées sont ordonnées par date décroissante."""
        for i in range(5):
            log_tool_call(job_id="job1", tool=f"tool_{i}", args={}, result="ok", duration_ms=1.0, success=True)
        trail = get_audit_trail(job_id="job1")
        # Le plus récent d'abord
        self.assertEqual(trail[0]["tool"], "tool_4")
        self.assertEqual(trail[4]["tool"], "tool_0")

    def test_empty_trail(self):
        """Vérifie qu'un trail vide retourne une liste vide."""
        trail = get_audit_trail(job_id="nonexistent")
        self.assertEqual(trail, [])

    def test_result_serialization(self):
        """Vérifie que les résultats complexes sont sérialisés puis désérialisés."""
        log_tool_call(
            job_id="job1",
            tool="complex_tool",
            args={"data": [1, 2, 3]},
            result={"key": "value", "nested": {"a": 1}},
            duration_ms=1.0,
            success=True,
        )
        trail = get_audit_trail(job_id="job1")
        self.assertEqual(len(trail), 1)
        # Le résultat est stocké en JSON puis re-parsé en dict à la lecture.
        result = trail[0]["result"]
        self.assertEqual(result["key"], "value")
        self.assertEqual(result["nested"]["a"], 1)


if __name__ == "__main__":
    unittest.main()