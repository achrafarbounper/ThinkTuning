import json

from agent.json_parser import extract_json_blocks
from agent.system_prompt import SYSTEM_PROMPT
from tools.tool_registry import TOOLS, REQUIRED_ARGS

# Plafond de taille du résultat injecté dans le prompt final (le contexte LLM
# n'est pas infini ; les tools tronquent déjà leurs sorties à ~8 Ko).
MAX_RESULT_CHARS = 4000
# Nombre maximum d'allers-retours LLM par requête utilisateur (planification +
# auto-corrections) : borne le coût et empêche les boucles infinies.
MAX_LLM_ROUNDS = 5


def _stringify(result) -> str:
    """Rend n'importe quel retour d'outil lisible et compact pour le LLM."""
    if isinstance(result, (dict, list)):
        try:
            text = json.dumps(result, ensure_ascii=False, indent=2, default=str)
        except Exception:
            text = str(result)
    else:
        text = str(result)
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + "\n… [tronqué]"
    return text


class AgentCore:
    def __init__(self, llm_client, max_rounds: int = MAX_LLM_ROUNDS):
        self.llm = llm_client
        self.max_rounds = max(2, int(max_rounds))

    def run(self, prompt: str):
        system = {"role": "system", "content": SYSTEM_PROMPT}
        messages = [system, {"role": "user", "content": prompt}]

        last_result = None
        problems: list[str] = []

        for _round in range(self.max_rounds):
            raw = self.llm.call(messages)
            messages.append({"role": "assistant", "content": raw})
            blocks = extract_json_blocks(raw)

            if not blocks:
                # Réponse en texte normal :
                # - rien n'a encore été exécuté -> réponse inexploitable telle
                #   quelle (contrat historique) ;
                # - sinon c'est la conclusion légitime de l'agent, enrichie de
                #   la trace des problèmes corrigés en cours de route.
                if last_result is None and not problems:
                    return f"Réponse non exploitable :\n{raw}"
                if problems:
                    raw += "\n\n[auto-correction] " + " | ".join(problems)
                return raw

            problem, last_result = self._execute(blocks, last_result)
            if problem is None:
                # Succès : le modèle choisit — conclure en texte OU enchaîner
                # avec l'appel suivant (ex : find_file PUIS read_file).
                messages.append({
                    "role": "user",
                    "content": (
                        f"Dernier résultat : {_stringify(last_result)}. "
                        "Si la tâche est complète, explique ce que tu as fait en "
                        "TEXTE NORMAL (sans JSON, sans tools). Sinon, renvoie le "
                        "prochain appel d'outil en UN SEUL JSON."
                    ),
                })
                continue

            problems.append(problem)
            # L'erreur repart au LLM : il peut corriger son appel au tour suivant
            # au lieu de laisser l'utilisateur avec un message technique brut.
            messages.append({"role": "user", "content": problem})

        # Budget de tours épuisé : forcer une conclusion en texte.
        conclusion = (
            f"Dernier résultat : {_stringify(last_result)}. "
            "Nombre maximum d'étapes atteint : conclus en TEXTE NORMAL, "
            "sans JSON, sans tools."
        )
        if problems:
            conclusion += " Problèmes survenus pendant l'exécution : " + " | ".join(problems)

        answer = self.llm.call([*messages, {"role": "user", "content": conclusion}])

        # Traçabilité pour l'utilisateur : les problèmes intermédiaires doivent
        # rester visibles même si le LLM omet de les mentionner.
        if problems:
            answer += "\n\n[auto-correction] " + " | ".join(problems)
        return answer

    @staticmethod
    def _normalize_args(tool: str, block):
        """Récupère les arguments d'un bloc JSON en tolérant les formats déviants.

        Format attendu : {"tool": ..., "args": {...}}. Les modèles légers écrivent
        parfois les arguments au niveau du bloc (appel « à plat » :
        {"tool": "read_file", "path": "..."}) ou fournissent la valeur brute quand
        le tool n'a qu'un seul argument obligatoire. Retourne None si le format
        reste incompréhensible : l'appelant signale alors l'erreur au LLM.

        Sans cette tolérance, un appel à plat déclenchait « Arguments manquants
        pour … : ['path'] » en boucle jusqu'à épuisement du budget de tours.
        """
        args = block.get("args")
        if isinstance(args, dict):
            return args
        if "args" not in block or args is None:
            # Appel à plat : tout ce qui n'est pas le nom du tool est un argument.
            return {k: v for k, v in block.items() if k != "tool"}
        required = REQUIRED_ARGS.get(tool, [])
        if isinstance(args, (str, int, float)) and len(required) == 1:
            # Valeur brute unique : {"args": "chemin"} -> {"path": "chemin"}.
            return {required[0]: args}
        return None

    def _execute(self, blocks, previous_result):
        """Exécute séquentiellement les blocs JSON ; s'arrête au premier problème.

        Retourne (problem, last_result) : problem=None si tous les appels ont
        réussi, sinon un message destiné AU LLM pour qu'il se corrige.
        """
        last_result = previous_result

        for block in blocks:
            if not isinstance(block, dict):
                return f"Bloc JSON invalide (objet attendu) : {block!r}.", last_result

            tool = block.get("tool")

            if tool not in TOOLS:
                available = ", ".join(sorted(TOOLS))
                return (
                    f"Tool inconnu : '{tool}'. Tools disponibles : {available}. "
                    "Renvoie UN SEUL JSON corrigé.",
                    last_result,
                )

            args = self._normalize_args(tool, block)
            if args is None:
                return (
                    f"'args' doit être un objet JSON pour '{tool}' "
                    f"(reçu : {block.get('args')!r}). Renvoie UN SEUL JSON corrigé.",
                    last_result,
                )

            missing = [k for k in REQUIRED_ARGS[tool] if k not in args]
            if missing:
                received = json.dumps(args, ensure_ascii=False)
                return (
                    f"Arguments manquants pour {tool} : {missing}. "
                    f"Arguments obligatoires : {REQUIRED_ARGS[tool]}. "
                    f"Reçu : {received}. Renvoie UN SEUL JSON corrigé sous la forme "
                    '{"tool": "<nom_du_tool>", "args": {...}}.',
                    last_result,
                )

            # Une exception d'outil (docker absent, réseau coupé, chemin hors
            # sandbox...) ne doit pas crasher l'agent : elle devient un message
            # que le LLM peut corriger au tour suivant.
            try:
                last_result = TOOLS[tool](**args)
            except Exception as exc:  # noqa: BLE001 - volontairement large
                detail = f"ERREUR pendant '{tool}' : {type(exc).__name__}: {exc}"
                hint = (
                    " Si un chemin de fichier est en cause, utilise find_file "
                    "pour le localiser avant de réessayer."
                    if tool in ("read_file", "write_file", "copy_path", "move_path")
                    else ""
                )
                return f"{detail}.{hint} Renvoie UN SEUL JSON corrigé.", last_result

        return None, last_result
