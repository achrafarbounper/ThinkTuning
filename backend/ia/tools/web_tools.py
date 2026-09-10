"""Outils Internet de l'agent : recherche web, récupération et lecture de pages.

Contrairement à http_get (réponse HTTP brute), ces outils renvoient du contenu
directement exploitable par le LLM :
    - web_search : recherche web en DEUX backends — instance SearXNG
      auto-hébergée en primaire (API JSON native) puis DuckDuckGo Lite en
      repli (HTML parsé avec html.parser de la bibliothèque standard) ;
      toute recherche bloquée (anti-bot…) est signalée par une clé 'error'
      explicite au lieu d'un « 0 résultat » ambigu ;
    - web_fetch  : page distante telle quelle (statut + corps BRUT tronqué) ;
    - web_read   : texte lisible extrait d'une page HTML (scripts, styles,
      balises supprimés), pour lire un article ou une documentation.

Sécurité (mêmes garde-fous que network_tools.py, P0 SEC F8) :
    - schémas http/https uniquement ;
    - protection SSRF ACTIVE PAR DÉFAUT (fail-closed), redirects suivis
      manuellement avec re-validation de l'hôte final (anti-bypass 302
      vers 169.254…) et corps bornés (anti-OOM) ;
    - timeouts plafonnés et sorties tronquées pour ne pas saturer le LLM.
"""

import os
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlparse

import requests

from .sandbox import (
    enforce_host_policy,
    enforce_response_host_policy,
    truncate_output,
    url_scheme_allowed,
)

DEFAULT_TIMEOUT_S = 20.0
DEFAULT_MAX_CHARS = 6000  # web_read : texte lisible injecté au LLM
FETCH_MAX_CHARS = 12000  # web_fetch : corps brut, plafond plus large
DEFAULT_MAX_RESULTS = 5
MAX_RESULTS_LIMIT = 10

# Endpoint « Lite » de DuckDuckGo : HTML statique simple, sans JavaScript requis.
SEARCH_ENDPOINT = "https://lite.duckduckgo.com/lite/"

# User-Agent de type navigateur : beaucoup de sites refusent un UA vide/inconnu.
_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) ThinkTuningAgent/1.0"
    ),
    "Accept-Language": "fr,en;q=0.8",
}

_TIMEOUT_MIN, _TIMEOUT_MAX = 1.0, 120.0
_PARSE_INPUT_LIMIT = 300_000  # jamais plus de ~300 Ko parsés par page
_MAX_REDIRECTS = 5  # suivi manuel des redirects avec re-validation SSRF

# --- Configuration des backends de recherche (env relues à chaque appel) ------------

# Backend primaire : instance SearXNG auto-hébergée — API JSON native, résultats
# agrégés de Google/Bing/Brave/DuckDuckGo… (search.formats doit contenir json).
# En compose : http://searxng:8080/search — en dev local : http://127.0.0.1:8888/search
SEARXNG_DEFAULT_URL = "http://127.0.0.1:8888/search"
_SEARCH_BACKENDS = ("auto", "searxng", "ddg")


def _searxng_url() -> str:
    """Endpoint /search de l'instance SearXNG (AGENT_SEARXNG_URL)."""
    return os.getenv("AGENT_SEARXNG_URL", "").strip() or SEARXNG_DEFAULT_URL


def _search_backend() -> str:
    """Ordre des backends (AGENT_SEARCH_BACKEND) : auto, searxng ou ddg."""
    raw = os.getenv("AGENT_SEARCH_BACKEND", "auto").strip().lower()
    return raw if raw in _SEARCH_BACKENDS else "auto"


def _search_language() -> str:
    """Langue demandée à SearXNG (AGENT_SEARCH_LANGUAGE ; vide = défaut instance)."""
    return os.getenv("AGENT_SEARCH_LANGUAGE", "fr").strip()


def _clean_timeout(timeout: float) -> float:
    return max(_TIMEOUT_MIN, min(float(timeout), _TIMEOUT_MAX))


def _clean_max_chars(max_chars: int) -> int:
    return max(50, int(max_chars))


# --- Parsing HTML (bibliothèque standard, zéro dépendance) ---------------------------


def _unwrap_ddg_redirect(href: str) -> str:
    """Déballe les liens réécrits par DuckDuckGo (/l/?uddg=<URL encodée>)."""
    href = (href or "").strip()
    if not href:
        return ""
    parsed = urlparse(href)
    if parsed.netloc.endswith("duckduckgo.com") or parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        if target:
            return target
    if href.startswith("//"):
        return "https:" + href  # URL relative au protocole
    return href


class _LiteResultsParser(HTMLParser):
    """Extrait (titre, url, extrait) des résultats de DuckDuckGo « Lite ».

    Structure du HTML lite : un <a class="result-link"> par résultat, suivi
    d'un <td class="result-snippet"> contenant l'extrait correspondant.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict] = []
        self._in_link = False
        self._link_href = ""
        self._link_text: list[str] = []
        self._in_snippet = False
        self._snippet_text: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = (attrs.get("class") or "").split()
        if tag == "a":
            self._flush_link()  # <a> jamais fermé -> ne rien perdre
            if "result-link" in classes:
                self._in_link = True
                self._link_href = attrs.get("href") or ""
                self._link_text = []
        elif tag == "td" and "result-snippet" in classes:
            self._in_snippet = True
            self._snippet_text = []

    def handle_endtag(self, tag):
        if tag == "a" and self._in_link:
            self._flush_link()
        elif tag == "td" and self._in_snippet:
            snippet = " ".join("".join(self._snippet_text).split())
            # Le snippet SUIT toujours son lien dans le HTML lite.
            if snippet and self.results:
                self.results[-1]["snippet"] = snippet
            self._in_snippet = False

    def handle_data(self, data):
        if self._in_link:
            self._link_text.append(data)
        elif self._in_snippet:
            self._snippet_text.append(data)

    def close(self):
        super().close()
        self._flush_link()

    def _flush_link(self):
        if not self._in_link:
            return
        title = " ".join("".join(self._link_text).split())
        url = _unwrap_ddg_redirect(self._link_href)
        self._in_link = False
        self._link_href = ""
        self._link_text = []
        if title and url:
            self.results.append({"title": title, "url": url, "snippet": ""})


class _ReadableTextParser(HTMLParser):
    """HTML -> texte lisible : scripts/styles exclus, blocs sur nouvelles lignes."""

    SKIP_TAGS = frozenset({"script", "style", "noscript", "template", "svg", "iframe"})
    BLOCK_TAGS = frozenset(
        {
            "address",
            "article",
            "aside",
            "blockquote",
            "br",
            "caption",
            "center",
            "div",
            "dd",
            "dl",
            "dt",
            "fieldset",
            "figcaption",
            "figure",
            "footer",
            "form",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "header",
            "hr",
            "li",
            "main",
            "nav",
            "ol",
            "p",
            "pre",
            "section",
            "table",
            "tbody",
            "td",
            "tfoot",
            "th",
            "thead",
            "tr",
            "ul",
        }
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.chunks: list[str] = []
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        elif tag in self.SKIP_TAGS:
            self._skip_depth += 1
        elif not self._skip_depth and tag in self.BLOCK_TAGS:
            self.chunks.append("\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag in self.SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif not self._skip_depth and tag in self.BLOCK_TAGS:
            self.chunks.append("\n")

    def handle_data(self, data):
        if self._skip_depth:
            return
        if self._in_title:
            self.title_parts.append(data)
        else:
            self._append_text(data)

    def _append_text(self, data: str) -> None:
        if not data.strip():
            return
        prev_last = self.chunks[-1][-1:] if self.chunks else ""
        # Espace inséré entre deux fragments collés (<b>mot</b><i>suite</i>).
        if prev_last.isalnum() and data[:1].isalnum():
            self.chunks.append(" ")
        self.chunks.append(data)

    def get_title(self) -> str:
        return " ".join("".join(self.title_parts).split())

    def get_text(self) -> str:
        lines = "".join(self.chunks).splitlines()
        cleaned = (" ".join(line.split()) for line in lines)
        return "\n".join(line for line in cleaned if line)


def _parsed_page(html_text: str) -> tuple[str, str]:
    """(title, texte_lisible) depuis du HTML ; entrée plafonnée pour la perf."""
    parser = _ReadableTextParser()
    parser.feed(html_text[:_PARSE_INPUT_LIMIT])
    parser.close()
    return parser.get_title(), parser.get_text()


def _request_page(url: str, headers: dict | None, timeout: float) -> requests.Response:
    """GET avec garde-fous schéma/SSRF + en-têtes navigateur fusionnés.

    P0 : suivi manuel des redirects avec re-validation SSRF + corps borné
    (anti-bypass 302 → 169.254… et anti-OOM) — cf. ``_secure_get``.
    """
    merged = dict(_HTTP_HEADERS)
    merged.update(dict(headers or {}))
    return _secure_get(url, headers=merged, timeout=_clean_timeout(timeout))


# --- SEARCH ------------------------------------------------------------------------


def _secure_get(
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
    timeout: float,
) -> requests.Response:
    """GET durci P0 (SSRF fail-closed + anti-redirect-bypass + borne taille).

    Suit manuellement ≤ _MAX_REDIRECTS sauts après re-validation SSRF de
    chaque URL, puis retourne la réponse finale AVEC ``resp.text`` borné à
    ``MAX_DOWNLOAD_BYTES`` (anti-OOM — les fakes de tests sans ``raw``
    retournent déjà du texte court, inchangé).
    """
    from urllib.parse import urljoin

    from .sandbox import MAX_DOWNLOAD_BYTES

    current = url
    current_params = params
    for _ in range(_MAX_REDIRECTS + 1):
        url_scheme_allowed(current)
        enforce_host_policy(current)
        resp = requests.get(
            current,
            params=current_params,
            headers=headers,
            timeout=timeout,
            allow_redirects=False,
        )
        current_params = None  # params déjà encodés dans l'URL après le 1er saut
        location = (resp.headers or {}).get("location")
        if resp.status_code not in (301, 302, 303, 307, 308) or not location:
            enforce_response_host_policy(getattr(resp, "url", None) or current)
            try:
                text = resp.text or ""
            except Exception:
                text = ""
            if len(text) > MAX_DOWNLOAD_BYTES:
                resp.text = text[:MAX_DOWNLOAD_BYTES]  # type: ignore[attr-defined]
            return resp
        current = urljoin(getattr(resp, "url", None) or current, location)
        enforce_host_policy(current)
    raise RuntimeError(f"Trop de redirects (>{_MAX_REDIRECTS}) pour : {url}")


def _searxng_search(query: str, max_results: int, timeout: float) -> dict:
    """Backend primaire : instance SearXNG auto-hébergée (API JSON native).

    Renvoie le payload standard {query, engine: 'searxng', result_count,
    results} ; en cas d'échec (instance absente, format json désactivé,
    réponse non JSON…), une clé 'error' explicite est ajoutée — jamais de
    « 0 résultat » ambigu. Les violations de la politique SSRF (sandbox)
    restent levées, comme pour les autres outils réseau.
    """
    payload: dict = {
        "query": query,
        "engine": "searxng",
        "result_count": 0,
        "results": [],
    }
    url = _searxng_url()
    url_scheme_allowed(url)
    enforce_host_policy(url)
    params: dict = {"q": query, "format": "json"}
    language = _search_language()
    if language:
        params["language"] = language
    try:
        resp = _secure_get(url, params=params, headers=dict(_HTTP_HEADERS), timeout=timeout)
    except requests.RequestException as exc:
        payload["error"] = f"SearXNG injoignable ({url}) : {exc}"
        return payload
    if resp.status_code >= 400:
        detail = {
            403: "format=json désactivé sur l'instance (search: formats dans searxng/settings.yml)",
            429: "rate limit de l'instance",
        }.get(resp.status_code, resp.reason)
        payload["error"] = f"SearXNG HTTP {resp.status_code} : {detail} ({url})"
        return payload
    try:
        data = resp.json()
    except ValueError as exc:
        payload["error"] = f"SearXNG : réponse non JSON ({url}) : {exc}"
        return payload

    seen_urls: set[str] = set()
    for item in data.get("results") or []:
        item_url = str(item.get("url") or "").strip()
        title = " ".join(str(item.get("title") or "").split())
        if not item_url or not title or item_url in seen_urls:
            continue  # doublons agrégés / entrées vides ignorés
        seen_urls.add(item_url)
        payload["results"].append(
            {
                "title": title,
                "url": item_url,
                "snippet": " ".join(str(item.get("content") or "").split()),
            }
        )
        if len(payload["results"]) >= max_results:
            break
    payload["result_count"] = len(payload["results"])
    # Moteurs amont en échec sur cette requête (utile pour diagnostiquer un
    # « 0 résultat » : SearXNG lui-même a pu être bloqué par ses moteurs).
    # SearXNG renvoie [moteur, raison] : affiché « moteur : raison ».
    unresponsive = data.get("unresponsive_engines") or []
    if unresponsive:
        payload["unresponsive_engines"] = [
            " : ".join(str(part) for part in entry)
            if isinstance(entry, (list, tuple))
            else str(entry)
            for entry in unresponsive[:10]
        ]
    return payload


def _ddg_lite_search(query: str, max_results: int, timeout: float) -> dict:
    """Backend de repli : DuckDuckGo Lite (HTML statique parsé).

    Aucune clé API requise ; parsing 100 % bibliothèque standard. Ne lève
    PAS sur HTTP >= 400 ni sur une page anti-bot : une entrée 'error' est
    renvoyée à la place, pour que l'agent puisse raisonner dessus.
    """
    payload: dict = {
        "query": query,
        "engine": "duckduckgo-lite",
        "result_count": 0,
        "results": [],
    }
    url_scheme_allowed(SEARCH_ENDPOINT)
    enforce_host_policy(SEARCH_ENDPOINT)
    # Le endpoint Lite n'accepte une requête qu'en POST : en GET il renvoie
    # un HTTP 202 « anomalie » sans résultats.
    try:
        resp = requests.post(
            SEARCH_ENDPOINT,
            data={"q": query},
            headers=dict(_HTTP_HEADERS),
            timeout=timeout,
            allow_redirects=False,
        )
        location = (resp.headers or {}).get("location")
        if resp.status_code in (301, 302, 303, 307, 308) and location:
            # Redirect DDG re-validé SSRF avant suivi (même politique que GET).
            from urllib.parse import urljoin

            target = urljoin(SEARCH_ENDPOINT, location)
            url_scheme_allowed(target)
            enforce_host_policy(target)
            resp = requests.post(
                target,
                data={"q": query},
                headers=dict(_HTTP_HEADERS),
                timeout=timeout,
                allow_redirects=False,
            )
        enforce_response_host_policy(getattr(resp, "url", None) or SEARCH_ENDPOINT)
        try:
            body = resp.text or ""
        except Exception:
            body = ""
        from .sandbox import MAX_DOWNLOAD_BYTES

        if len(body) > MAX_DOWNLOAD_BYTES:
            resp.text = body[:MAX_DOWNLOAD_BYTES]  # type: ignore[attr-defined]
    except requests.RequestException as exc:
        payload["error"] = f"DuckDuckGo injoignable : {exc}"
        return payload

    if resp.status_code >= 400:
        payload["error"] = f"Recherche impossible : HTTP {resp.status_code} ({resp.reason})."
        return payload
    # Page « anomalie » anti-bot (observée en réel : HTTP 202 + formulaire
    # anomaly.js?cc=botnet) : DDG refuse la requête sans aucun résultat. On
    # le signale explicitement — un « 0 résultat » silencieux ferait croire
    # au LLM qu'aucune réponse n'existe sur le sujet.
    body = resp.text[:_PARSE_INPUT_LIMIT]
    if resp.status_code == 202 or "anomaly.js" in body or "cc=botnet" in body:
        payload["error"] = (
            f"DuckDuckGo a bloqué la requête (anti-bot, HTTP {resp.status_code}, "
            "page anomalie) : réessayez plus tard ou configurez une instance "
            "SearXNG (AGENT_SEARXNG_URL)."
        )
        return payload

    parser = _LiteResultsParser()
    parser.feed(body)
    parser.close()
    # Liens publicitaires exclus : ce sont les seuls « résultats » dont l'URL
    # finale reste sur duckduckgo.com (/y.js?ad_domain=...) après déballage.
    found = [
        item
        for item in parser.results
        if not urlparse(item["url"]).netloc.lower().endswith("duckduckgo.com")
    ]
    payload["results"] = found[:max_results]
    payload["result_count"] = len(payload["results"])
    if len(found) > len(payload["results"]):
        payload["truncated"] = True
    return payload


def web_search(
    query: str, max_results: int = DEFAULT_MAX_RESULTS, timeout: float = DEFAULT_TIMEOUT_S
) -> dict:
    """Recherche web : SearXNG (primaire) puis DuckDuckGo Lite (repli).

    Renvoie {query, engine, result_count, results} — 'engine' indique le
    backend qui a produit les résultats ('searxng' ou 'duckduckgo-lite').
    Chaque résultat est {title, url, snippet}. AGENT_SEARCH_BACKEND choisit
    l'ordre : auto (défaut), searxng (sans repli) ou ddg (SearXNG ignorée).
    En mode auto, un échec SearXNG déclenche le repli ; le payload porte
    alors 'searxng_error', et 'error' si les DEUX backends échouent.
    """
    query = str(query or "").strip()
    if not query:
        raise ValueError("'query' ne peut pas être vide.")
    max_results = max(1, min(int(max_results), MAX_RESULTS_LIMIT))
    timeout = _clean_timeout(timeout)
    backend = _search_backend()

    if backend in ("auto", "searxng"):
        payload = _searxng_search(query, max_results, timeout)
        # Résultat exploitable : pas d'erreur ET soit des résultats, soit un
        # vrai « 0 résultat » (moteurs amont tous répondu). Si SearXNG répond
        # 0 résultat alors que tous ses moteurs sont en échec, on tente le
        # repli — la réponse vide n'est alors pas fiable.
        usable = "error" not in payload and (
            payload["result_count"] > 0 or not payload.get("unresponsive_engines")
        )
        if backend == "searxng" or usable:
            return payload
        searxng_error = payload.get("error")
    else:
        searxng_error = None

    fallback = _ddg_lite_search(query, max_results, timeout)
    if searxng_error:
        fallback["searxng_error"] = searxng_error
        if "error" in fallback:
            fallback["error"] = f"{searxng_error} | Repli DuckDuckGo : {fallback['error']}"
    return fallback


# --- FETCH -------------------------------------------------------------------------


def web_fetch(
    url: str,
    headers: dict | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    max_chars: int = FETCH_MAX_CHARS,
) -> dict:
    """Récupère une page distante : {status, reason, url, content_type, title, body}.

    Comme http_get : ne lève PAS sur 4xx/5xx, le code HTTP est retourné tel
    quel pour que l'agent puisse raisonner dessus. Le corps est renvoyé BRUT
    (HTML éventuel), tronqué à max_chars ; pour du texte lisible, préférer
    web_read.
    """
    resp = _request_page(url, headers, timeout)
    content_type = resp.headers.get("content-type") or ""
    title = None
    if "html" in content_type.lower():
        try:
            title, _ = _parsed_page(resp.text)
        except Exception:
            title = None  # HTML malformé : le corps reste exploitable tel quel
    return {
        "status": resp.status_code,
        "reason": resp.reason,
        "url": resp.url,
        "content_type": content_type or None,
        "title": title or None,
        "body": truncate_output(resp.text, _clean_max_chars(max_chars)),
    }


# --- READ --------------------------------------------------------------------------


def web_read(
    url: str, timeout: float = DEFAULT_TIMEOUT_S, max_chars: int = DEFAULT_MAX_CHARS
) -> dict:
    """Lit une page web et en extrait le TEXTE lisible (sans HTML).

    Scripts, styles et balises sont supprimés ; titres, paragraphes et listes
    deviennent des lignes distinctes. Idéal avant de raisonner sur un article,
    une documentation ou une page de résultats.
    """
    resp = _request_page(url, None, timeout)
    try:
        title, text = _parsed_page(resp.text)
    except Exception as exc:
        raise RuntimeError(f"Lecture impossible (HTML invalide ?) : {exc}") from exc
    clean = truncate_output(text, _clean_max_chars(max_chars))
    return {
        "status": resp.status_code,
        "url": resp.url,
        "content_type": resp.headers.get("content-type") or None,
        "title": title or None,
        "char_count": len(clean),
        "text": clean,
    }
