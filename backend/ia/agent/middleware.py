"""Pipeline de middlewares pour les appels d'outils de l'agent.

Un middleware est une fonction qui intercepte l'appel à un outil avant et/ou
après son exécution. Il peut :
    - Modifier les arguments avant l'appel
    - Modifier le résultat après l'appel
    - Court-circuiter l'appel (retourner sans exécuter)
    - Logger, auditer, ou appliquer des politiques de sécurité

Ordre d'exécution : les middlewares sont exécutés par priorité croissante
(0 = premier, 100 = dernier). Le résultat de chaque middleware est passé au suivant.

Exemple :
    def my_middleware(ctx: ToolContext, next_call: Callable) -> Any:
        print(f"Avant {ctx.tool_name}")
        ctx.args["timestamp"] = time.time()
        result = next_call(ctx)
        print(f"Après {ctx.tool_name}")
        return result

    register_middleware(my_middleware, priority=10)
"""

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("thinktuning.agent.middleware")


@dataclass
class ToolContext:
    """Contexte d'exécution d'un outil, transmis à travers le pipeline.

    Attributes:
        tool_name: nom de l'outil appelé
        args: arguments de l'outil (modifiables)
        result: résultat de l'outil (après exécution)
        error: exception si erreur (après exécution)
        skipped: True si l'appel doit être court-circuité
        metadata: données libres pour les middlewares
    """
    tool_name: str
    args: Dict[str, Any]
    result: Any = None
    error: Optional[Exception] = None
    skipped: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)


MiddlewareFunc = Callable[[ToolContext, Callable[[ToolContext], Any]], Any]


# ============================================================
# Registre des middlewares
# ============================================================

_middlewares: List[tuple[int, MiddlewareFunc]] = []
_middleware_lock = threading.Lock()


def register_middleware(func: MiddlewareFunc, priority: int = 50) -> None:
    """Enregistre un middleware dans le pipeline.

    Args:
        func: fonction middleware (ctx, next_call) -> result
        priority: ordre d'exécution (0=premier, 100=dernier, défaut=50)
    """
    with _middleware_lock:
        _middlewares.append((priority, func))
        _middlewares.sort(key=lambda x: x[0])
        logger.debug("middleware: registered '%s' with priority %d", func.__name__, priority)


def unregister_middleware(func: MiddlewareFunc) -> None:
    """Supprime un middleware du pipeline."""
    with _middleware_lock:
        _middlewares[:] = [(p, f) for p, f in _middlewares if f is not func]


def clear_middlewares() -> None:
    """Supprime tous les middlewares (pour les tests)."""
    with _middleware_lock:
        _middlewares.clear()


def get_middlewares() -> List[tuple[int, MiddlewareFunc]]:
    """Retourne la liste des middlewares enregistrés (copie)."""
    with _middleware_lock:
        return list(_middlewares)


def process_tool_call(
    tool_name: str,
    args: Dict[str, Any],
    executor: Callable[[Dict[str, Any]], Any],
) -> Any:
    """Exécute un appel d'outil à travers le pipeline de middlewares.

    Args:
        tool_name: nom de l'outil
        args: arguments de l'outil
        executor: fonction d'exécution réelle (args) -> result

    Returns:
        Le résultat de l'outil (éventuellement modifié par un middleware)

    Raises:
        Exception: relancée si un middleware ou l'outil lève une exception
    """
    with _middleware_lock:
        middlewares = list(_middlewares)

    ctx = ToolContext(tool_name=tool_name, args=dict(args))

    # Construire la chaîne d'appel
    def _execute_tool(context: ToolContext) -> Any:
        """Appel final : exécute l'outil réel."""
        if context.skipped:
            logger.debug("middleware: tool '%s' skipped by middleware", tool_name)
            return context.result
        return executor(context.args)

    # Envelopper avec les middlewares (en ordre inverse pour le chaînage)
    chain = _execute_tool
    for priority, mw_func in reversed(middlewares):
        chain = _make_middleware_chain(mw_func, chain, priority)

    return chain(ctx)


def _make_middleware_chain(
    mw_func: MiddlewareFunc,
    next_in_chain: Callable[[ToolContext], Any],
    priority: int,
) -> Callable[[ToolContext], Any]:
    """Crée une fermeture qui enveloppe un middleware avec le suivant."""

    def _wrapped(ctx: ToolContext) -> Any:
        try:
            return mw_func(ctx, next_in_chain)
        except Exception as exc:
            logger.warning(
                "middleware: error in '%s' (priority %d): %s",
                getattr(mw_func, "__name__", repr(mw_func)),
                priority,
                exc,
            )
            raise

    return _wrapped


# ============================================================
# Middlewares intégrés
# ============================================================

def logging_middleware(ctx: ToolContext, next_call: Callable) -> Any:
    """Middleware de logging : trace chaque appel d'outil."""
    logger.info("tool_call: %s args=%s", ctx.tool_name, ctx.args)
    result = next_call(ctx)
    if ctx.error:
        logger.warning("tool_error: %s error=%s", ctx.tool_name, ctx.error)
    else:
        logger.info("tool_result: %s", ctx.tool_name)
    return result


def timing_middleware(ctx: ToolContext, next_call: Callable) -> Any:
    """Middleware de chronométrage : mesure la durée d'exécution."""
    import time
    start = time.perf_counter()
    result = next_call(ctx)
    duration_ms = (time.perf_counter() - start) * 1000.0
    ctx.metadata["duration_ms"] = duration_ms
    return result
