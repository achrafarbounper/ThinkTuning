"""Outil calculatrice sûre pour l'agent IA.

Évalue une expression arithmétique SANS exec/eval : l'expression est parsée
avec le module `ast` puis parcourue par un évaluateur maison qui n'accepte que
les nœuds explicitement autorisés (nombres, + - * / // % **, parens, unaire ±).
Tout autre construct (nom de variable, appel de fonction, attribut, lambda…)
est refusé : aucun code arbitraire ne peut être exécuté.

Utile à l'agent pour raisonner chiffré : batch × epochs, taux de réussite,
moyennes de métriques, conversions d'unités…
"""

import ast


_MAX_EXPONENT = 10_000  # borne anti-bombes à exposants (9**9**9…)

_ALLOWED_BINOPS = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow)
_ALLOWED_UNARYOPS = (ast.UAdd, ast.USub)


def _eval_node(node: ast.AST) -> float:
    """Évalue récursivement un nœud AST whitelisté."""
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)

    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ValueError(f"Seuls les nombres sont autorisés, pas : {node.value!r}")
        return node.value

    if isinstance(node, ast.BinOp):
        if not isinstance(node.op, _ALLOWED_BINOPS):
            raise ValueError(f"Opérateur interdit : {type(node.op).__name__}")
        left = _eval_node(node.left)
        right = _eval_node(node.right)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, (ast.Div, ast.FloorDiv, ast.Mod)):
            try:
                if isinstance(node.op, ast.Div):
                    return left / right
                if isinstance(node.op, ast.FloorDiv):
                    return left // right
                return left % right
            except ZeroDivisionError as exc:
                raise ValueError("Division par zéro.") from exc
        # Pow : borne sur l'exposant pour éviter les calculs géants
        if abs(right) > _MAX_EXPONENT:
            raise ValueError(f"Exposant trop grand (|{right:g}| > {_MAX_EXPONENT}).")
        try:
            return left**right
        except OverflowError as exc:
            raise ValueError("Résultat trop grand pour être représenté.") from exc

    if isinstance(node, ast.UnaryOp):
        if not isinstance(node.op, _ALLOWED_UNARYOPS):
            raise ValueError(f"Opérateur unaire interdit : {type(node.op).__name__}")
        value = _eval_node(node.operand)
        return -value if isinstance(node.op, ast.USub) else +value

    raise ValueError(
        "Construction interdite dans une expression arithmétique : "
        f"{type(node).__name__}. Nombres, + - * / // % ** et parenthèses seulement."
    )


def calc(expression: str) -> dict:
    """Évalue une expression arithmétique pure et renvoie {expression, result}.

    Exemples valides : "(3 + 4) * 2", "2 ** 10", "17 // 5", "-1.5 * 8".
    Tout appel (`__import__`, `open`…), nom de variable ou chaîne est refusé.
    """
    if not isinstance(expression, str) or not expression.strip():
        raise ValueError("'expression' doit être une chaîne arithmétique non vide.")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"Expression invalide : {exc.msg}") from exc

    result = _eval_node(tree)
    # Rend le résultat plus naturel : 6.0 -> 6 quand il n'y a pas de perte.
    if isinstance(result, float) and result.is_integer():
        result = int(result)
    return {"expression": expression.strip(), "result": result}