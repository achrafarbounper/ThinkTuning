"""Boucle de l'agent : planification JSON -> exécution d'outils -> réponse finale.

Correctifs du bug « l'agent ne montre pas le contenu du fichier » :
    1. AUTO-CORRECTION : réponse sans JSON, tool inconnu ou arguments manquants
       sont renvoyés AU LLM (jusqu'à MAX_TOOL_ROUNDS tentatives) avec la liste
       des outils valides, au lieu de terminer le run sur une erreur brute ;
    2. FIDÉLITÉ : la réponse finale doit reproduire le résultat de l'outil tel
       quel et n'a plus le droit d'inventer un contenu quand le résultat est
       vide ou en erreur ;
    3. Le system prompt est généré depuis le registre des outils (voir
       agent/system_prompt.py) : plus de dérive prompt <-> réalité.
"""

import json
import logging

from agent.json_parser import extract_json_blocks
from agent.system_prompt import SYSTEM_PROMPT
from tools.tool_registry import TOOLS, REQUIRED_ARGS

logger = logging.getLogger(__name__)

# Plafond de taille du résultat injecté dans le prompt final (le contexte LLM
# n'est pas infini ; les tools tronquent déjà leurs sorties à ~8 Ko).
MAX_RESULT_CHARS = 4000
# Nombre maximal de tentatives pour obtenir/exécuter un appel d'outil valide.
MAX_TOOL_ROUNDS = 3
# En-tête du prompt de synthèse (les tests offline s'y accrochent).
FINAL_HEADER = "Dernier résultat"


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

def _final_instructions() -> str:
    """Consignes de fidélité de la réponse finale (anti-hallucination)."""
    return (
        "Rédige maintenant la réponse finale pour l’utilisateur en TEXTE NORMAL "
        "(pas de JSON, pas de nom de tool).\n"
        "RÈGLES DE FIDÉLITÉ (obligatoires) :\n"
        "- Si le résultat contient un contenu de fichier, du code ou des données, "
        "reproduis la partie pertinente TELLE QUELLE, entre triples backticks.\n"
        "- N’invente JAMAIS un contenu absent du résultat : si le résultat est "
        "une erreur, vide ou None, dis-le clairement.\n"
        "- Reste concis sur ce que tu as fait, mais complet sur le résultat."
    )




class AgentCore:
    def __init__(self, llm_client):
        self.llm = llm_client

    @staticmethod
    def _correction_message(problem: str) -> dict:
        """Message renvoyé au LLM pour qu'il corrige son appel d'outil."""
        return {
            "role": "user",
            "content": (
                f"[auto-correction] {problem}\n"
                f"Tools disponibles : {sorted(TOOLS)}.\n"
                'Renvoie UN SEUL JSON corrigé : {"tool": "nom", "args": {...}} — '
                "sans texte avant ni après."
            ),
        }

    def run(self, prompt: str):
        logger.info("Nouveau run de l'agent : %.200s", prompt)
        system = {"role": "system", "content": SYSTEM_PROMPT}
        history = [system, {"role": "user", "content": prompt}]

        last_result = None
        last_raw = ""
        # État en sortie de boucle :
        #   "done"         au moins un outil exécuté (ou erreur d'outil à expliquer)
        #   "unknown_tool" le LLM a insisté sur un outil inexistant
        #   "bad_args"     arguments requis toujours manquants
        #   "no_json"      aucune réponse exploitable
        outcome = "no_json"
        unknown_tool = ""
        missing_args = ""

        for round_index in range(1, MAX_TOOL_ROUNDS + 1):
            logger.info("Tour %d/%d : appel du LLM (%d message(s) dans l'historique)...",
                        round_index, MAX_TOOL_ROUNDS, len(history))
            raw = self.llm.call(history)
            last_raw = raw
            blocks = extract_json_blocks(raw)

            if not blocks:
                logger.warning(
                    "Tour %d/%d : aucun objet JSON exploitable dans la réponse du LLM.",
                    round_index,
                    MAX_TOOL_ROUNDS,
                )
                history.append({"role": "assistant", "content": raw})
                history.append(
                    self._correction_message(
                        "Ta réponse ne contient aucun objet JSON exploitable."
                    )
                )
                outcome = "no_json"
                continue

            planning_error = ""
            for block in blocks:
                if not isinstance(block, dict):
                    planning_error = 'Chaque appel doit être un objet JSON {"tool", "args"}.'
                    break

                tool = block.get("tool")
                args = block.get("args") if isinstance(block.get("args"), dict) else {}

                if tool not in TOOLS:
                    unknown_tool = str(tool)
                    planning_error = f"Tool inconnu : '{tool}'."
                    break

                missing = [key for key in REQUIRED_ARGS[tool] if key not in args]
                if missing:
                    missing_args = f"Arguments manquants pour {tool} : {missing}"
                    planning_error = missing_args
                    break

                # Une exception d'outil (docker absent, réseau coupé, chemin hors
                # sandbox...) ne crash pas l'agent : convertie en message que le
                # LLM expliquera à l'utilisateur (comportement historique conservé).
                try:
                    logger.info("Exécution du tool '%s' avec %d argument(s).", tool, len(args))
                    last_result = TOOLS[tool](**args)
                    logger.info("Tool '%s' terminé.", tool)
                except Exception as exc:  # noqa: BLE001 - volontairement large
                    last_result = f"ERREUR pendant '{tool}' : {type(exc).__name__}: {exc}"
                    logger.warning("Échec de l'outil '%s' : %s", tool, last_result)

                outcome = "done"

            if planning_error:
                logger.warning(
                    "Tour %d/%d : %s", round_index, MAX_TOOL_ROUNDS, planning_error
                )
                history.append({"role": "assistant", "content": raw})
                history.append(self._correction_message(planning_error))
                outcome = (
                    "unknown_tool"
                    if unknown_tool
                    else "bad_args"
                    if missing_args
                    else "no_json"
                )
                continue
            break  # au moins un outil exécuté -> passage à la synthèse

        if outcome != "done":
            suffix = f" (après {MAX_TOOL_ROUNDS} tentatives d'auto-correction)"
            if outcome == "unknown_tool":
                return (
                    f"Tool inconnu : '{unknown_tool}'. "
                    f"Tools disponibles : {sorted(TOOLS)}{suffix}."
                )
            if outcome == "bad_args":
                return f"{missing_args}{suffix}."
            return f"Réponse non exploitable :\n{last_raw}"

        history.append({"role": "assistant", "content": last_raw})
        history.append(
            {
                "role": "user",
                "content": (
                    f"{FINAL_HEADER} : {_stringify(last_result)}.\n"
                    f"{_final_instructions()}"
                ),
            }
        )
        final_answer = self.llm.call(history)
        logger.info("Réponse finale générée (%d caractères).", len(final_answer))
        return final_answer
