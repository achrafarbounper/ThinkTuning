"""Outils Internet de l'agent : recherche web, récupération et lecture de pages.

Contrairement à http_get (réponse HTTP brute), ces outils renvoient du contenu
directement exploitable par le LLM :
    - web_search : résultats de recherche DuckDuckGo (titre, URL, extrait),
      parsés avec html.parser de la bibliothèque standard — AUCUNE dépendance
      supplémentaire ;
    - web_fetch  : page distante telle quelle (statut + corps BRUT tronqué) ;
    - web_read   : texte lisible extrait d'une page HTML (scripts, styles,
      balises supprimés), pour lire un article ou une documentation.

Sécurité (mêmes garde-fous que network_tools.py) :
    - schémas http/https uniquement ;
    - politique anti-SSRF optionnelle : AGENT_BLOCK_PRIVATE_HOSTS=1 interdit
      les hôtes privés/loopback ;
    - timeouts plafonnés et sorties tronquées pour ne pas saturer le LLM.
"""

from html.parser import HTMLParser
from urllib.parse import parse_qs, urlparse

import requests

from .sandbox import enforce_host_policy, truncate_output, url_scheme_allowed

DEFAULT_TIMEOUT_S = 20.0
DEFAULT_MAX_CHARS = 6000   # web_read : texte lisible injecté au LLM
FETCH_MAX_CHARS = 12000    # web_fetch : corps brut, plafond plus large
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
    BLOCK_TAGS = frozenset({
        "address", "article", "aside", "blockquote", "br", "caption", "center",
        "div", "dd", "dl", "dt", "fieldset", "figcaption", "figure", "footer",
        "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li",
        "main", "nav", "ol", "p", "pre", "section", "table", "tbody", "td",
        "tfoot", "th", "thead", "tr", "ul",
    })

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
    """GET avec garde-fous schéma/SSRF + en-têtes navigateur fusionnés."""
    url_scheme_allowed(url)
    enforce_host_policy(url)
    merged = dict(_HTTP_HEADERS)
    merged.update(dict(headers or {}))
    return requests.get(url, headers=merged, timeout=_clean_timeout(timeout))


# --- SEARCH ------------------------------------------------------------------------

def web_search(query: str, max_results: int = DEFAULT_MAX_RESULTS,
               timeout: float = DEFAULT_TIMEOUT_S) -> dict:
    """Recherche web DuckDuckGo : {query, engine, result_count, results}.

    Chaque résultat est {title, url, snippet}. Aucune clé API requise ; le
    parsing utilise uniquement la bibliothèque standard. Ne lève PAS sur
    HTTP >= 400 : une entrée 'error' est renvoyée à la place, pour que
    l'agent puisse raisonner dessus (blocage, rate limit…).
    """
    query = str(query or "").strip()
    if not query:
        raise ValueError("'query' ne peut pas être vide.")
    max_results = max(1, min(int(max_results), MAX_RESULTS_LIMIT))

    url_scheme_allowed(SEARCH_ENDPOINT)
    enforce_host_policy(SEARCH_ENDPOINT)
    # Le endpoint Lite n'accepte une requête qu'en POST : en GET il renvoie
    # un HTTP 202 « anomalie » sans résultats.
    resp = requests.post(
        SEARCH_ENDPOINT, data={"q": query}, headers=dict(_HTTP_HEADERS),
        timeout=_clean_timeout(timeout),
    )

    payload: dict = {
        "query": query,
        "engine": "duckduckgo-lite",
        "result_count": 0,
        "results": [],
    }
    if resp.status_code >= 400:
        payload["error"] = (
            f"Recherche impossible : HTTP {resp.status_code} ({resp.reason})."
        )
        return payload

    parser = _LiteResultsParser()
    parser.feed(resp.text[:_PARSE_INPUT_LIMIT])
    parser.close()
    # Liens publicitaires exclus : ce sont les seuls « résultats » dont l'URL
    # finale reste sur duckduckgo.com (/y.js?ad_domain=...) après déballage.
    found = [
        item for item in parser.results
        if not urlparse(item["url"]).netloc.lower().endswith("duckduckgo.com")
    ]
    payload["results"] = found[:max_results]
    payload["result_count"] = len(payload["results"])
    if len(found) > len(payload["results"]):
        payload["truncated"] = True
    return payload


# --- FETCH -------------------------------------------------------------------------

def web_fetch(url: str, headers: dict | None = None,
              timeout: float = DEFAULT_TIMEOUT_S,
              max_chars: int = FETCH_MAX_CHARS) -> dict:
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

def web_read(url: str, timeout: float = DEFAULT_TIMEOUT_S,
             max_chars: int = DEFAULT_MAX_CHARS) -> dict:
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