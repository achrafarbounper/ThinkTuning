"""Client HTTP multi-provider entièrement streamé (`stream: true`).

Deux providers sont supportés :

    - ``ollama``     (défaut) : endpoint chat Ollama, flux NDJSON, champs
      « message.content » / « message.thinking », fenêtre de contexte
      transmise via ``options.num_ctx`` ;
    - ``openrouter`` : endpoint compatible OpenAI
      (https://openrouter.ai/api/v1/chat/completions), flux SSE
      (« data: {...} » jusqu'à « data: [DONE] »), fragments dans
      ``choices[0].delta.{content, reasoning}`` (repli « reasoning_content »).

Les événements sont publiés sur le logger « thinktuning.agent » (même canal
que AgentCore) : requête/durée/statut en INFO, contenus complets en DEBUG,
erreurs réseau en ERROR. Le niveau se règle via AGENT_LOG_LEVEL.

Tous les appels utilisent ``stream: true`` ; ``call_stream()`` consomme le
flux ligne à ligne, en émettant chaque fragment de texte au fil de l'eau via
les callbacks ``on_thinking`` / ``on_content``. ``call()`` conserve son type
de retour historique (str) pour ne rien casser côté appelants et tests, mais
désormais la génération est réellement diffusée en amont.

Mode « Réflexion » (think=True) : côté Ollama le paramètre « think » est
envoyé et la trace de raisonnement (champ natif « message.thinking », ou
balises <think> inline en repli pour les serveurs anciens) est exposée sur
``LLMClient.last_thinking`` après chaque appel. Côté OpenRouter les modèles
de raisonnement émettent spontanément ``delta.reasoning`` ; la trace arrive
donc naturellement sans paramètre dédié.
"""

import json
import logging
import os
import time
from typing import Callable, Optional

import requests

# Extraction des balises <think> inline : repli quand le serveur Ollama ne
# sépare pas lui-même la réflexion dans le champ « message.thinking ».
from .thinking import extract_thinking

logger = logging.getLogger("thinktuning.agent")
logger.setLevel(os.getenv("AGENT_LOG_LEVEL", "INFO").upper())

# Providers supportés par le client.
PROVIDERS = ("ollama", "openrouter")

# Température appliquée quand l'appelant n'en fournit pas explicitement
# (0.8 = défaut historique du serveur Ollama).
DEFAULT_TEMPERATURE = 0.8

# Taille de fenêtre de contexte (tokens) appliquée par défaut à chaque appel,
# transmise à Ollama via `options.num_ctx`. 2048 = défaut historique d'Ollama ;
# on l'envoie désormais explicitement pour un comportement stable et
# reproductible quelle que soit la configuration côté serveur.
DEFAULT_CONTEXT_LENGTH = 2048


def _parse_chunk(line: str):
    """Parse une ligne du flux NDJSON renvoyé par Ollama (`stream: true`).

    Tolère un préfixe éventuel « data: » (certains proxys SSE) et renvoie
    ``None`` pour toute ligne hors format (ligne vide, ``[DONE]``, JSON
    invalide…) sans jamais lever d'exception.
    """
    # `iter_lines(decode_unicode=True)` renvoie ENCORE des `bytes` quand Ollama
    # n'expose pas de charset dans son Content-Type (application/x-ndjson) :
    # on normalise systématiquement en `str` avant tout traitement.
    if isinstance(line, (bytes, bytearray)):
        line = line.decode("utf-8", errors="replace")
    line = line.strip()
    if line.startswith("data:"):
        line = line[len("data:"):].lstrip()
    if not line or line == "[DONE]":
        return None
    try:
        return json.loads(line)
    except (ValueError, TypeError):
        return None


class LLMClient:
    def __init__(
        self,
        url,
        model,
        timeout=None,
        temperature=None,
        think=False,
        context_length=None,
        provider="ollama",
        api_key=None,
    ):
        self.url = url
        self.model = model
        # Provider LLM : « ollama » (défaut historique) ou « openrouter ».
        # Valeur inconnue -> erreur immédiate (typo détectée au démarrage).
        provider = (provider or "ollama").strip().lower()
        if provider not in PROVIDERS:
            raise ValueError(
                f"Provider LLM inconnu : {provider!r} (supportés : {', '.join(PROVIDERS)})"
            )
        self.provider = provider
        # Clé API portée en « Authorization: Bearer » (requis par OpenRouter,
        # ignoré par Ollama qui reste sans authentification).
        self.api_key = api_key
        # Timeout en secondes pour requests.post ; None = comportement historique
        # (attente indéfinie), utilisé par ia/main.py.
        self.timeout = timeout
        self.temperature = DEFAULT_TEMPERATURE if temperature is None else float(temperature)
        # Fenêtre de contexte (tokens) envoyée à Ollama via `options.num_ctx`.
        # None -> DEFAULT_CONTEXT_LENGTH (2048) ; on force explicitement la
        # valeur par défaut pour ne plus dépendre de la config côté serveur.
        self.context_length = (
            DEFAULT_CONTEXT_LENGTH if context_length is None else int(context_length)
        )
        # Mode « Réflexion » : demande à Ollama de séparer le raisonnement du
        # modèle dans le champ « message.thinking » (deepseek-r1, qwen3…).
        # Le paramètre n'est PAS envoyé quand False : les modèles sans support
        # natif et les serveurs Ollama anciens ne doivent pas être perturbés.
        self.think = bool(think)
        # Trace de réflexion du DERNIER appel ("" si aucune) ; lue par
        # AgentCore via getattr(llm, "last_thinking", "").
        self.last_thinking = ""

    def call(self, messages):
        """Appel historique : renvoie la réponse complète (str).

        Depuis le passage au streaming, ``call()`` consomme le flux Ollama
        (`stream: true`) via ``call_stream()`` et réassemble la réponse — le
        contrat de retour est inchangé pour les appelants et les tests.
        """
        return self.call_stream(messages)

    def call_stream(
        self,
        messages,
        on_thinking: Optional[Callable[[str], None]] = None,
        on_content: Optional[Callable[[str], None]] = None,
    ):
        """Appelle Ollama en streaming (`stream: true`) et réassemble la réponse.

        Args:
            messages:    historique de la conversation (format OpenAI).
            on_thinking: callback optionnel, invoqué dès qu'un fragment de la
                         trace « message.thinking » arrive (temps réel).
            on_content:  callback optionnel, invoqué dès qu'un fragment du
                         contenu arrive (temps réel).

        Returns:
            Le contenu complet de la réponse (str), balises <think> retirées.
            ``self.last_thinking`` porte la trace de réflexion accumulée.
        """
        started = time.perf_counter()
        logger.info(
            "llm_request provider=%s url=%s model=%s messages=%d timeout=%s streaming=true num_ctx=%d",
            self.provider,
            self.url,
            self.model,
            len(messages),
            self.timeout,
            self.context_length,
        )
        if self.provider == "openrouter":
            # Format compatible OpenAI : température au niveau racine, pas de
            # bloc « options » (num_ctx n'existe pas côté OpenRouter ; la
            # fenêtre de contexte est gérée par le modèle hébergé).
            payload = {
                "model": self.model,
                "messages": messages,
                "stream": True,
                "temperature": self.temperature,
            }
            # Les modèles de raisonnement d'OpenRouter émettent spontanément
            # delta.reasoning : aucun paramètre « think » à envoyer.
        else:
            payload = {
                "model": self.model,
                "messages": messages,
                "stream": True,
                "options": {
                    # Fenêtre de contexte du modèle (tokens). Explicite pour ne pas
                    # dépendre du défaut du serveur Ollama (2048 par défaut ici).
                    "num_ctx": self.context_length,
                    # Température avant d'être réellement transmise à Ollama : le
                    # champ était historiquement configuré mais jamais envoyé.
                    "temperature": self.temperature,
                },
            }
        if self.think and self.provider == "ollama":
            # Support natif du « thinking » côté Ollama (>0.9) : le flux porte
            # alors un champ « message.thinking » séparé du contenu.
            payload["think"] = True

        # OpenRouter exige la clé API en Bearer ; Ollama part sans entête
        # d'authentification. Sans clé, l'appel garde SA FORME HISTORIQUE
        # (pas d'argument « headers ») : certains appelants/tests monkeypatchent
        # requests.post avec une signature stricte.
        if self.api_key:
            headers = {"Authorization": f"Bearer {self.api_key}"}
        else:
            headers = None

        try:
            if headers is not None:
                resp = requests.post(
                    self.url,
                    json=payload,
                    headers=headers,
                    timeout=self.timeout,
                    stream=True,
                )
            else:
                resp = requests.post(
                    self.url, json=payload, timeout=self.timeout, stream=True
                )
            resp.raise_for_status()
        except requests.exceptions.Timeout:
            logger.error(
                "llm_timeout url=%s model=%s elapsed_ms=%.0f timeout=%s",
                self.url,
                self.model,
                (time.perf_counter() - started) * 1000,
                self.timeout,
            )
            raise
        except requests.exceptions.HTTPError:
            status = resp.status_code if resp is not None else "?"
            logger.error("llm_http_error url=%s status=%s", self.url, status)
            raise
        except requests.exceptions.RequestException:
            logger.exception("llm_connection_error url=%s", self.url)
            raise

        # Consommation du flux. Deux formats tolérés :
        #   - Ollama (NDJSON) : chaque ligne porte « message » ou « delta », avec
        #     les champs « content » et/ou « thinking » selon le type de token ;
        #   - OpenAI/OpenRouter (SSE « data: {...} ») : fragments dans
        #     choices[0].delta.{content, reasoning} (repli « reasoning_content »).
        # _parse_chunk retire déjà le préfixe « data: » et ignore [DONE].
        content_parts: list[str] = []
        thinking_parts: list[str] = []
        try:
            for line in resp.iter_lines(decode_unicode=True):
                if not line:
                    continue
                chunk = _parse_chunk(line)
                if chunk is None:
                    continue
                msg = chunk.get("message") or chunk.get("delta") or {}
                delta = msg.get("content") or ""
                think = msg.get("thinking") or ""
                choices = chunk.get("choices") or []
                if choices:
                    oa_delta = choices[0].get("delta") or {}
                    think = (
                        think
                        or oa_delta.get("reasoning")
                        or oa_delta.get("reasoning_content")
                        or ""
                    )
                    delta = delta or oa_delta.get("content") or ""
                if delta:
                    content_parts.append(delta)
                    if on_content is not None:
                        on_content(delta)
                if think:
                    thinking_parts.append(think)
                    if on_thinking is not None:
                        on_thinking(think)
                if chunk.get("done"):
                    break
        finally:
            resp.close()

        content = "".join(content_parts)
        thinking = "".join(thinking_parts).strip()

        # Repli : des serveurs anciens (ou des modèles qui ignorent le
        # paramètre « think ») laissent les balises <think> inline dans le
        # contenu au lieu du champ « message.thinking ».
        content, inline_thinking = extract_thinking(content)
        parts = [part for part in (inline_thinking, thinking) if part]
        self.last_thinking = "\n\n".join(parts)

        logger.info(
            "llm_response status=%d elapsed_ms=%.0f content_chars=%d thinking_chars=%d",
            resp.status_code,
            (time.perf_counter() - started) * 1000,
            len(content),
            len(self.last_thinking),
        )
        logger.debug("llm_response_content=%s", content)
        if self.last_thinking:
            logger.debug("llm_response_thinking=%s", self.last_thinking)
        return content
