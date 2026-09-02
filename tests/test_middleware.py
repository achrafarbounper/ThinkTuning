"""Tests unitaires pour le pipeline de middlewares.

Vérifie :
    - Enregistrement et désenregistrement de middlewares
    - Ordre d'exécution par priorité
    - Modification des arguments et résultats
    - Court-circuit d'appel
    - Gestion des erreurs dans les middlewares
"""

import unittest

from ia.agent.middleware import (
    ToolContext,
    register_middleware,
    unregister_middleware,
    clear_middlewares,
    get_middlewares,
    process_tool_call,
)


class TestMiddleware(unittest.TestCase):
    """Tests du pipeline de middlewares."""

    def setUp(self):
        """Réinitialise les middlewares avant chaque test."""
        clear_middlewares()

    def test_register_and_execute(self):
        """Vérifie l'enregistrement et l'exécution d'un middleware."""
        calls = []

        def mw(ctx, next_call):
            calls.append(("before", ctx.tool_name))
            result = next_call(ctx)
            calls.append(("after", result))
            return result

        register_middleware(mw, priority=50)
        result = process_tool_call("test_tool", {"x": 1}, lambda a: "executed")
        self.assertEqual(result, "executed")
        self.assertEqual(calls, [("before", "test_tool"), ("after", "executed")])

    def test_priority_order(self):
        """Vérifie que les middlewares sont exécutés par priorité croissante."""
        order = []

        def mw1(ctx, next_call):
            order.append("mw1_before")
            result = next_call(ctx)
            order.append("mw1_after")
            return result

        def mw2(ctx, next_call):
            order.append("mw2_before")
            result = next_call(ctx)
            order.append("mw2_after")
            return result

        register_middleware(mw1, priority=10)
        register_middleware(mw2, priority=20)
        process_tool_call("tool", {}, lambda a: "ok")
        self.assertEqual(order, ["mw1_before", "mw2_before", "mw2_after", "mw1_after"])

    def test_modify_args(self):
        """Vérifie qu'un middleware peut modifier les arguments."""

        def mw(ctx, next_call):
            ctx.args["modified"] = True
            return next_call(ctx)

        register_middleware(mw, priority=10)
        received = {}

        def executor(a):
            received.update(a)
            return "done"

        process_tool_call("tool", {"original": 1}, executor)
        self.assertEqual(received, {"original": 1, "modified": True})

    def test_modify_result(self):
        """Vérifie qu'un middleware peut modifier le résultat."""

        def mw(ctx, next_call):
            result = next_call(ctx)
            return f"wrapped({result})"

        register_middleware(mw, priority=10)
        result = process_tool_call("tool", {}, lambda a: "original")
        self.assertEqual(result, "wrapped(original)")

    def test_skip_execution(self):
        """Vérifie qu'un middleware peut court-circuiter l'appel."""

        def mw(ctx, next_call):
            ctx.skipped = True
            ctx.result = "skipped_result"
            return next_call(ctx)

        register_middleware(mw, priority=10)
        called = []

        def executor(a):
            called.append(True)
            return "should_not_happen"

        result = process_tool_call("tool", {}, executor)
        self.assertEqual(result, "skipped_result")
        self.assertEqual(called, [])

    def test_unregister_middleware(self):
        """Vérifie la désinscription d'un middleware."""
        calls = []

        def mw(ctx, next_call):
            calls.append(1)
            return next_call(ctx)

        register_middleware(mw, priority=10)
        process_tool_call("tool", {}, lambda a: "ok")
        self.assertEqual(len(calls), 1)
        unregister_middleware(mw)
        process_tool_call("tool", {}, lambda a: "ok")
        self.assertEqual(len(calls), 1)

    def test_clear_middlewares(self):
        """Vérifie la suppression de tous les middlewares."""
        register_middleware(lambda ctx, next: next(ctx), priority=10)
        register_middleware(lambda ctx, next: next(ctx), priority=20)
        self.assertEqual(len(get_middlewares()), 2)
        clear_middlewares()
        self.assertEqual(len(get_middlewares()), 0)

    def test_no_middlewares(self):
        """Vérifie que l'exécution fonctionne sans middleware."""
        result = process_tool_call("tool", {"x": 1}, lambda a: a["x"] * 2)
        self.assertEqual(result, 2)

    def test_middleware_error_propagates(self):
        """Vérifie qu'une erreur dans un middleware est propagée."""

        def bad_mw(ctx, next_call):
            raise ValueError("middleware error")

        register_middleware(bad_mw, priority=10)
        with self.assertRaises(ValueError) as ctx:
            process_tool_call("tool", {}, lambda a: "ok")
        self.assertEqual(str(ctx.exception), "middleware error")

    def test_multiple_middlewares_chain(self):
        """Vérifie le chaînage de plusieurs middlewares."""

        def mw1(ctx, next_call):
            ctx.args["chain"] = ctx.args.get("chain", "") + "A"
            return next_call(ctx)

        def mw2(ctx, next_call):
            ctx.args["chain"] = ctx.args.get("chain", "") + "B"
            return next_call(ctx)

        register_middleware(mw1, priority=10)
        register_middleware(mw2, priority=20)
        received = {}

        def executor(a):
            received.update(a)
            return "ok"

        process_tool_call("tool", {}, executor)
        self.assertEqual(received.get("chain"), "AB")


class TestToolContext(unittest.TestCase):
    """Tests de la dataclass ToolContext."""

    def test_default_values(self):
        """Vérifie les valeurs par défaut."""
        ctx = ToolContext(tool_name="test", args={"x": 1})
        self.assertEqual(ctx.tool_name, "test")
        self.assertEqual(ctx.args, {"x": 1})
        self.assertIsNone(ctx.result)
        self.assertIsNone(ctx.error)
        self.assertFalse(ctx.skipped)
        self.assertEqual(ctx.metadata, {})

    def test_custom_values(self):
        """Vérifie la personnalisation des valeurs."""
        ctx = ToolContext(
            tool_name="test",
            args={},
            result="done",
            error=None,
            skipped=True,
            metadata={"key": "value"},
        )
        self.assertEqual(ctx.result, "done")
        self.assertTrue(ctx.skipped)
        self.assertEqual(ctx.metadata, {"key": "value"})


if __name__ == "__main__":
    unittest.main()