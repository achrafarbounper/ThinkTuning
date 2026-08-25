import json

from agent.json_parser import extract_json_blocks
from agent.system_prompt import SYSTEM_PROMPT
from tools.tool_registry import TOOLS, REQUIRED_ARGS

# Plafond de taille du résultat injecté dans le prompt final (le contexte LLM
# n'est pas infini ; les tools tronquent déjà leurs sorties à ~8 Ko).
MAX_RESULT_CHARS = 4000


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
    def __init__(self, llm_client):
        self.llm = llm_client

    def run(self, prompt: str):
        system = {"role": "system", "content": SYSTEM_PROMPT}

        raw = self.llm.call([system, {"role": "user", "content": prompt}])
        blocks = extract_json_blocks(raw)

        if not blocks:
            return f"Réponse non exploitable :\n{raw}"

        last_result = None

        for block in blocks:
            tool = block.get("tool")
            args = block.get("args", {})

            if tool not in TOOLS:
                return f"Tool inconnu : {tool}"

            missing = [k for k in REQUIRED_ARGS[tool] if k not in args]
            if missing:
                return f"Arguments manquants pour {tool} : {missing}"

            # Une exception d'outil (docker absent, réseau coupé, chemin hors
            # sandbox...) ne doit pas crasher l'agent : elle est convertie en
            # message que le LLM expliquera à l'utilisateur.
            try:
                last_result = TOOLS[tool](**args)
            except Exception as exc:  # noqa: BLE001 - volontairement large
                last_result = f"ERREUR pendant '{tool}' : {type(exc).__name__}: {exc}"
                break

        final_prompt = (
            f"Dernier résultat : {_stringify(last_result)}. "
            "Explique ce que tu as fait en texte normal, sans JSON, sans tools."
        )

        return self.llm.call([system, {"role": "user", "content": final_prompt}])
