"""Tests offline des outils Internet de l'agent (ia/tools/web_tools.py).

Aucun réseau réel : HTTP simulé (monkeypatch de requests.get/post), comme
tests/test_agent_tools.py. Couvre :
    - SearXNG nominal (JSON -> {title, url, snippet}, dédoublonnage, langue) ;
    - SearXNG en échec (403 format json, injoignable, moteurs amont KO)
      -> repli DuckDuckGo Lite en mode auto, erreur explicite sinon ;
    - DuckDuckGo Lite : parsing des résultats, filtre publicités, page
      « anomalie » anti-bot (HTTP 202 / anomaly.js?cc=botnet) -> 'error' ;
    - politique SSRF : blocage des hôtes privés + exemption allowlist.

Lance avec : pytest tests/test_web_tools.py -v
"""

import pytest
from ia.tools import sandbox
from ia.tools import web_tools


# --- Fakes HTTP ----------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status=200, text="", url="http://example.test/x",
                 json_payload=None):
        self.status_code = status
        self.reason = "OK" if status < 400 else "Err"
        self.text = text
        self.url = url
        self.headers = {"content-type": "text/html; charset=utf-8"}
        self._json = json_payload

    def json(self):
        if self._json is None:
            raise ValueError("pas de JSON")
        return self._json


class _FakeHtmlResponse(_FakeResponse):
    def __init__(self, text, status=200, url="http://example.test/x"):
        super().__init__(status=status, text=text, url=url)


# --- Fixtures HTML / JSON ------------------------------------------------------------

_SEARXNG_JSON = {
    "query": "python tutorial",
    "results": [
        {"url": "https://docs.python.org/3/", "title": "Python 3 docs",
         "content": "Documentation officielle.", "engine": "duckduckgo",
         "score": 2.0},
        {"url": "https://realpython.com/", "title": "Real Python",
         "content": "Tutoriels Python.", "engine": "google", "score": 1.5},
        {"url": "https://docs.python.org/3/", "title": "Doublon agrégé",
         "content": "même URL vue par un autre moteur", "engine": "bing"},
        {"url": "", "title": "Entrée sans URL", "content": ""},
    ],
    "unresponsive_engines": [],
}

_PAGE_HTML = """
<html>
  <head><title>Page de test</title><style>body { color: red; }</style></head>
  <body>
    <h1>Titre principal</h1>
    <p>Premier paragraphe utile.</p>
    <script>console.log('secret');</script>
    <p>Deuxième &amp; dernier paragraphe.</p>
  </body>
</html>
"""

_LITE_HTML = """
<html><body><table>
<tr><td>&nbsp;1.&nbsp;</td></tr>
<tr><td class="result-link"><a rel="nofollow" class="result-link"
      href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.python.org%2F3%2F&amp;rut=sig">Python 3 docs</a></td></tr>
<tr><td class="result-snippet">Documentation officielle.</td></tr>
<tr><td class="result-link"><a rel="nofollow" class="result-link"
      href="https://realpython.com/">Real Python</a></td></tr>
<tr><td class="result-snippet">Tutoriels Python.</td></tr>
</table></body></html>
"""

# Page « anomalie » anti-bot renvoyée par DDG (observée en réel : HTTP 202,
# formulaire pointant vers anomaly.js?...&cc=botnet) — aucun résultat dedans.
_ANOMALY_HTML = """
<html><head><title>DuckDuckGo</title></head><body>
<div class="lite_wrapper">
  <a class="header-url" href="/lite/"><span class="header">DuckDuckGo</span></a>
  <iframe name="ifr" width="0" height="0" class="hidden"></iframe>
  <form id="img-form"
        action="//duckduckgo.com/anomaly.js?sv=lite&amp;cc=botnet&amp;ti=1788652212&amp;q=test">
    <input type="text" name="q" value="test">
  </form>
</div>
</body></html>
"""


@pytest.fixture()
def backend_auto(monkeypatch):
    """Mode auto (SearXNG primaire) avec une instance factice hors réseau."""
    monkeypatch.setenv("AGENT_SEARCH_BACKEND", "auto")
    monkeypatch.setenv("AGENT_SEARXNG_URL", "http://192.168.1.50:8888/search")
    return monkeypatch


@pytest.fixture()
def backend_ddg(backend_auto):
    """Backend DuckDuckGo seul (SearXNG ignorée)."""
    backend_auto.setenv("AGENT_SEARCH_BACKEND", "ddg")
    return backend_auto


# --- Backend SearXNG (primaire) --------------------------------------------------------

def test_searxng_results_mapped_and_dedup(backend_auto, monkeypatch):
    seen = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        seen.update(url=url, params=params)
        return _FakeResponse(json_payload=_SEARXNG_JSON)

    monkeypatch.setattr(web_tools.requests, "get", fake_get)
    result = web_tools.web_search("python tutorial")

    assert result["engine"] == "searxng" and result["result_count"] == 2
    assert "error" not in result
    premier, second = result["results"]
    assert premier["url"] == "https://docs.python.org/3/"   # doublon agrégé dédupliqué
    assert premier["snippet"] == "Documentation officielle."
    assert second["url"] == "https://realpython.com/"
    assert seen["params"]["format"] == "json"               # API JSON demandée
    assert seen["params"]["q"] == "python tutorial"
    assert seen["params"]["language"] == "fr"               # langue par défaut

def test_searxng_max_results_limit(backend_auto, monkeypatch):
    many = {"results": [
        {"url": f"https://site{i}.test/", "title": f"Site {i}", "content": ""}
        for i in range(9)
    ]}
    monkeypatch.setattr(
        web_tools.requests, "get", lambda *a, **k: _FakeResponse(json_payload=many)
    )
    result = web_tools.web_search("test", max_results=3)
    assert result["result_count"] == 3


def test_searxng_403_json_disabled_falls_back_to_ddg(backend_auto, monkeypatch):
    """format=json désactivé (403) -> repli DuckDuckGo + 'searxng_error' tracé."""
    monkeypatch.setattr(
        web_tools.requests, "get", lambda *a, **k: _FakeResponse(status=403)
    )
    monkeypatch.setattr(
        web_tools.requests, "post",
        lambda *a, **k: _FakeHtmlResponse(
            _LITE_HTML, url="https://lite.duckduckgo.com/lite/"
        ),
    )
    result = web_tools.web_search("python tutorial")
    assert result["engine"] == "duckduckgo-lite"          # le repli a répondu
    assert result["result_count"] == 2
    assert "403" in result["searxng_error"]
    assert "SearXNG" in result["searxng_error"]
    assert "error" not in result                          # le repli a réussi


def test_searxng_backend_only_returns_error_without_fallback(backend_auto, monkeypatch):
    """AGENT_SEARCH_BACKEND=searxng : pas de repli silencieux vers DuckDuckGo."""
    monkeypatch.setenv("AGENT_SEARCH_BACKEND", "searxng")

    def fail_post(*a, **k):
        raise AssertionError("le repli DDG ne doit pas être appelé")

    monkeypatch.setattr(
        web_tools.requests, "get", lambda *a, **k: _FakeResponse(status=403)
    )
    monkeypatch.setattr(web_tools.requests, "post", fail_post)
    result = web_tools.web_search("test")
    assert result["engine"] == "searxng"
    assert "403" in result["error"] and "format=json" in result["error"]


def test_searxng_unreachable_falls_back(backend_auto, monkeypatch):
    def fail_get(*a, **k):
        raise web_tools.requests.ConnectionError("connexion refusée")

    monkeypatch.setattr(web_tools.requests, "get", fail_get)
    monkeypatch.setattr(
        web_tools.requests, "post",
        lambda *a, **k: _FakeHtmlResponse(_LITE_HTML),
    )
    result = web_tools.web_search("test")
    assert result["engine"] == "duckduckgo-lite" and result["result_count"] == 2
    assert "SearXNG injoignable" in result["searxng_error"]


def test_searxng_zero_results_with_dead_engines_is_not_trusted(backend_auto, monkeypatch):
    """SearXNG répond 0 résultat mais TOUS ses moteurs sont KO -> repli DDG."""
    payload = {"results": [], "unresponsive_engines": ["google, bing, duckduckgo"]}
    monkeypatch.setattr(
        web_tools.requests, "get", lambda *a, **k: _FakeResponse(json_payload=payload)
    )
    monkeypatch.setattr(
        web_tools.requests, "post",
        lambda *a, **k: _FakeHtmlResponse(_LITE_HTML),
    )
    result = web_tools.web_search("test")
    assert result["engine"] == "duckduckgo-lite" and result["result_count"] == 2


def test_searxng_legit_zero_results_kept(backend_auto, monkeypatch):
    """0 résultat sans moteur KO = vrai « pas de résultat » : pas de repli."""

    def fail_post(*a, **k):
        raise AssertionError("repli inutile pour un vrai 0 résultat")

    monkeypatch.setattr(
        web_tools.requests, "get",
        lambda *a, **k: _FakeResponse(json_payload={"results": []}),
    )
    monkeypatch.setattr(web_tools.requests, "post", fail_post)
    result = web_tools.web_search("motintrouvablexyzzy")
    assert result["engine"] == "searxng" and result["result_count"] == 0
    assert "error" not in result


def test_searxng_unresponsive_engines_reported(backend_auto, monkeypatch):
    payload = {
        "results": [{"url": "https://a.test/", "title": "A", "content": "a"}],
        "unresponsive_engines": ["google (timeout)", ["bing", "captcha"]],
    }
    monkeypatch.setattr(
        web_tools.requests, "get", lambda *a, **k: _FakeResponse(json_payload=payload)
    )
    result = web_tools.web_search("test")
    assert result["unresponsive_engines"] == [
        "google (timeout)", "bing : captcha",   # [moteur, raison] formaté
    ]


def test_searxng_non_json_response_reports_error(backend_auto, monkeypatch):
    monkeypatch.setenv("AGENT_SEARCH_BACKEND", "searxng")
    monkeypatch.setattr(
        web_tools.requests, "get",
        lambda *a, **k: _FakeHtmlResponse("<html>surprise</html>"),
    )
    result = web_tools.web_search("test")
    assert "non JSON" in result["error"]


def test_both_backends_fail_reports_combined_error(backend_auto, monkeypatch):
    """Les deux backends échouent : les DEUX erreurs restent visibles."""

    def fail_get(*a, **k):
        raise web_tools.requests.ConnectionError("connexion refusée")

    monkeypatch.setattr(web_tools.requests, "get", fail_get)
    monkeypatch.setattr(
        web_tools.requests, "post",
        lambda *a, **k: _FakeHtmlResponse(
            _ANOMALY_HTML, status=202, url="https://lite.duckduckgo.com/lite/"
        ),
    )
    result = web_tools.web_search("test")
    assert result["result_count"] == 0
    assert "SearXNG injoignable" in result["error"]
    assert "DuckDuckGo" in result["error"]
    assert result["searxng_error"].startswith("SearXNG injoignable")


def test_invalid_backend_env_behaves_like_auto(backend_auto, monkeypatch):
    """AGENT_SEARCH_BACKEND invalide -> repli sur le comportement « auto »."""
    monkeypatch.setenv("AGENT_SEARCH_BACKEND", "bogus")

    def fail_get(*a, **k):
        raise web_tools.requests.ConnectionError("connexion refusée")

    monkeypatch.setattr(web_tools.requests, "get", fail_get)
    monkeypatch.setattr(
        web_tools.requests, "post",
        lambda *a, **k: _FakeHtmlResponse(_LITE_HTML),
    )
    result = web_tools.web_search("test")
    assert result["engine"] == "duckduckgo-lite" and result["result_count"] == 2

# --- Backend DuckDuckGo Lite (repli) ---------------------------------------------------

def test_ddg_parses_results(backend_ddg, monkeypatch):
    seen = {}

    def fake_post(url, data=None, headers=None, timeout=None):
        seen.update(url=url, data=data)
        return _FakeHtmlResponse(_LITE_HTML, url="https://lite.duckduckgo.com/lite/")

    monkeypatch.setattr(web_tools.requests, "post", fake_post)
    result = web_tools.web_search("python tutorial")

    assert seen["data"] == {"q": "python tutorial"}   # requête envoyée en POST
    assert result["engine"] == "duckduckgo-lite" and result["result_count"] == 2
    premier, second = result["results"]
    assert premier["url"] == "https://docs.python.org/3/"   # lien uddg « déballé »
    assert premier["title"] == "Python 3 docs"
    assert premier["snippet"] == "Documentation officielle."
    assert second["url"] == "https://realpython.com/"


def test_ddg_max_results_and_empty_query(backend_ddg, monkeypatch):
    monkeypatch.setattr(
        web_tools.requests, "post", lambda *a, **k: _FakeHtmlResponse(_LITE_HTML)
    )
    result = web_tools.web_search("python", max_results=1)
    assert result["result_count"] == 1 and result["truncated"] is True
    with pytest.raises(ValueError, match="vide"):
        web_tools.web_search("   ")


def test_ddg_reports_http_error_without_raising(backend_ddg, monkeypatch):
    monkeypatch.setattr(
        web_tools.requests, "post",
        lambda *a, **k: _FakeHtmlResponse("<html></html>", status=403),
    )
    result = web_tools.web_search("test")
    assert result["result_count"] == 0 and "403" in result["error"]


def test_ddg_filters_ad_links(backend_ddg, monkeypatch):
    """Les annonces (URL finale restée sur duckduckgo.com/y.js) sont exclues."""
    html = (
        "<html><body><table>"
        '<tr><td class="result-link"><a class="result-link" '
        'href="https://duckduckgo.com/y.js?ad_domain=shop.example">Super promo</a></td></tr>'
        '<tr><td class="result-snippet">Publicité.</td></tr>'
        '<tr><td class="result-link"><a class="result-link" '
        'href="https://docs.python.org/">Python docs</a></td></tr>'
        '<tr><td class="result-snippet">Doc officielle.</td></tr>'
        "</table></body></html>"
    )
    monkeypatch.setattr(
        web_tools.requests, "post", lambda *a, **k: _FakeHtmlResponse(html)
    )
    result = web_tools.web_search("achat")
    assert result["result_count"] == 1
    assert result["results"][0]["url"] == "https://docs.python.org/"
    assert result["results"][0]["snippet"] == "Doc officielle."


def test_ddg_anomaly_page_202_reports_error(backend_ddg, monkeypatch):
    """Page anti-bot HTTP 202 (anomaly.js?cc=botnet) -> 'error' explicite."""
    monkeypatch.setattr(
        web_tools.requests, "post",
        lambda *a, **k: _FakeHtmlResponse(
            _ANOMALY_HTML, status=202, url="https://lite.duckduckgo.com/lite/"
        ),
    )
    result = web_tools.web_search("test")
    assert result["result_count"] == 0
    assert "anti-bot" in result["error"] and "202" in result["error"]


def test_ddg_anomaly_marker_on_http_200(backend_ddg, monkeypatch):
    """Marqueurs anomaly.js/cc=botnet même en HTTP 200 -> 'error'."""
    monkeypatch.setattr(
        web_tools.requests, "post", lambda *a, **k: _FakeHtmlResponse(_ANOMALY_HTML)
    )
    result = web_tools.web_search("test")
    assert "anti-bot" in result["error"]


def test_ddg_legit_empty_results_is_not_an_error(backend_ddg, monkeypatch):
    """Vraie page « aucun résultat » (200, sans marqueur) : pas d'erreur."""
    monkeypatch.setattr(
        web_tools.requests, "post",
        lambda *a, **k: _FakeHtmlResponse("<html><body>No results.</body></html>"),
    )
    result = web_tools.web_search("xzcyv")
    assert result["result_count"] == 0
    assert "error" not in result

# --- Politique SSRF (sandbox) ------------------------------------------------------------

def test_web_search_blocks_private_hosts(backend_auto, monkeypatch):
    """Même politique SSRF que http_get : SearXNG privée refusée si flag actif."""
    monkeypatch.setenv("AGENT_BLOCK_PRIVATE_HOSTS", "1")
    monkeypatch.delenv("AGENT_PRIVATE_HOST_ALLOWLIST", raising=False)
    with pytest.raises(PermissionError, match="privé"):
        web_tools.web_search("test")   # instance factice 192.168.1.50 (privée)


def test_web_search_private_host_allowlist_exemption(backend_auto, monkeypatch):
    """AGENT_PRIVATE_HOST_ALLOWLIST autorise l'instance SearXNG de confiance."""
    monkeypatch.setenv("AGENT_BLOCK_PRIVATE_HOSTS", "1")
    monkeypatch.setenv("AGENT_PRIVATE_HOST_ALLOWLIST", "192.168.1.50")
    monkeypatch.setattr(
        web_tools.requests, "get",
        lambda *a, **k: _FakeResponse(json_payload=_SEARXNG_JSON),
    )
    result = web_tools.web_search("python tutorial")
    assert result["engine"] == "searxng" and result["result_count"] == 2


def test_private_host_allowlist_parsing(monkeypatch):
    monkeypatch.setenv(
        "AGENT_PRIVATE_HOST_ALLOWLIST", " 127.0.0.1, localhost ,SEARXNG ,"
    )
    assert sandbox.get_private_host_allowlist() == {"127.0.0.1", "localhost", "searxng"}
    monkeypatch.delenv("AGENT_PRIVATE_HOST_ALLOWLIST")
    assert sandbox.get_private_host_allowlist() == set()


def test_enforce_host_policy_allowlist_exemption(monkeypatch):
    monkeypatch.setenv("AGENT_BLOCK_PRIVATE_HOSTS", "1")
    monkeypatch.delenv("AGENT_PRIVATE_HOST_ALLOWLIST", raising=False)
    with pytest.raises(PermissionError):
        sandbox.enforce_host_policy("http://127.0.0.1:8888/search")
    monkeypatch.setenv("AGENT_PRIVATE_HOST_ALLOWLIST", "127.0.0.1")
    sandbox.enforce_host_policy("http://127.0.0.1:8888/search")  # ne lève pas


# --- web_fetch / web_read (HTTP simulé) ---------------------------------------------------

def test_web_read_extracts_readable_text(monkeypatch):
    monkeypatch.setattr(
        web_tools.requests, "get", lambda *a, **k: _FakeHtmlResponse(_PAGE_HTML)
    )
    result = web_tools.web_read("http://example.test/article")
    assert result["status"] == 200 and result["title"] == "Page de test"
    assert "Titre principal" in result["text"]
    assert "Premier paragraphe utile." in result["text"]
    assert "Deuxième & dernier" in result["text"]      # entités décodées
    assert "console.log" not in result["text"]         # scripts supprimés
    assert "color: red" not in result["text"]          # styles supprimés


def test_web_fetch_returns_structured_page(monkeypatch):
    seen = {}

    def fake_get(url, headers=None, timeout=None):
        seen.update(headers=headers)
        return _FakeHtmlResponse(_PAGE_HTML)

    monkeypatch.setattr(web_tools.requests, "get", fake_get)
    result = web_tools.web_fetch(
        "http://example.test/page", headers={"X-Custom": "1"}, max_chars=200
    )
    assert seen["headers"]["X-Custom"] == "1"           # en-têtes fusionnés
    assert "User-Agent" in seen["headers"]              # UA navigateur ajouté
    assert result["status"] == 200 and result["title"] == "Page de test"
    assert "<h1>" in result["body"]                     # corps BRUT
    assert "tronqué" in result["body"]                  # plafond max_chars appliqué


def test_web_fetch_and_read_block_bad_scheme():
    from ia.tools.web_tools import web_fetch, web_read

    with pytest.raises(ValueError, match="Schéma interdit"):
        web_fetch("ftp://example.test/f")
    with pytest.raises(ValueError, match="Schéma interdit"):
        web_read("file:///etc/passwd")
