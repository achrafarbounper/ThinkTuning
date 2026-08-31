/**
 * clientCore.js
 * ---------------------------------------------------------------------
 * Cœur HTTP minimal et « critique » du client API ThinkTuning.
 *
 * C'est la SEULE partie indispensable au premier rendu : toute l'application
 * consomme ce transport (fetch) dès le montage (via AppProvider). Le reste du
 * client (endpoints métier) vit dans sentimentApiClient.js, et les helpers
 * agent + étapes de jobs dans des modules dédiés : cela maintient petit le
 * bundle initial chargé sur le chemin critique.
 *
 * Exposé ici :
 *   - ApiError                   (erreur HTTP normalisée)
 *   - DEFAULT_BASE_URL           (base lue depuis VITE_API_URL)
 *   - DEFAULT_TIMEOUT_MS         (30 s, JSON)
 *   - DEFAULT_MULTIPART_TIMEOUT_MS (60 s, uploads CSV)
 *   - SentimentApiClientCore     (transport : _request, _requestMultipart, …)
 */

export const DEFAULT_BASE_URL =
  (typeof import.meta !== "undefined" &&
    import.meta.env &&
    import.meta.env.VITE_API_URL) ??
  "http://localhost:8000";

// --- Timeout réseau -----------------------------------------------------------

/** Délai par défaut d'une requête JSON (30 s). */
export const DEFAULT_TIMEOUT_MS = 30_000;
/** Délai par défaut d'un upload multipart (60 s, fichiers CSV potentiellement lourds). */
export const DEFAULT_MULTIPART_TIMEOUT_MS = 60_000;

export class ApiError extends Error {
  constructor(message, status, detail) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

/**
 * Classe « natte » du transport HTTP : en-têtes, construction d'URL, requêtes
 * JSON et multipart avec timeout + support d'annulation externe (AbortSignal).
 * Les endpoints métier l'étendent (voir sentimentApiClient.js).
 */
export class SentimentApiClientCore {
  constructor({ baseUrl = DEFAULT_BASE_URL, apiKey = "" } = {}) {
    this.baseUrl = baseUrl.replace(/\/+$/, "");
    this.apiKey = apiKey || "";
  }

  setConfig({ baseUrl, apiKey } = {}) {
    if (baseUrl !== undefined) this.baseUrl = baseUrl.replace(/\/+$/, "");
    if (apiKey !== undefined) this.apiKey = apiKey;
  }

  _headers(isJson) {
    const headers = {};
    if (isJson) headers["Content-Type"] = "application/json";
    if (this.apiKey) headers["X-API-Key"] = this.apiKey;
    return headers;
  }

  _buildUrl(path, query) {
    let url = `${this.baseUrl}${path}`;
    if (query) {
      const entries = Object.entries(query).filter(
        ([, v]) => v !== undefined && v !== null && v !== ""
      );
      if (entries.length) {
        url += `?${new URLSearchParams(entries).toString()}`;
      }
    }
    return url;
  }

  async _request(path, { method = "GET", body, query, timeoutMs, signal } = {}) {
    const url = this._buildUrl(path, query);
    const init = { method, headers: this._headers(body !== undefined) };
    if (body !== undefined) init.body = JSON.stringify(body);

    const { controller, cleanup } = this._startRequest(timeoutMs, signal);
    init.signal = controller.signal;

    let response;
    try {
      response = await fetch(url, init);
    } catch (networkErr) {
      throw new ApiError(
        this._networkErrorMessage(
          networkErr,
          controller.signal.reason === "timeout"
        ),
        0,
        null
      );
    } finally {
      cleanup();
    }

    if (response.status === 204) return null;

    const contentType = response.headers.get("content-type") || "";
    const isJson = contentType.includes("application/json");
    const payload = isJson
      ? await response.json().catch(() => null)
      : await response.text();

    if (!response.ok) {
      const detail = isJson && payload ? payload.detail : payload;
      throw new ApiError(
        typeof detail === "string" && detail
          ? detail
          : `Erreur HTTP ${response.status} sur ${path}`,
        response.status,
        detail
      );
    }

    return payload;
  }

  async _requestMultipart(path, { formData, query, expectBlob = false, timeoutMs, signal } = {}) {
    const url = this._buildUrl(path, query);
    const headers = this.apiKey ? { "X-API-Key": this.apiKey } : {};

    const { controller, cleanup } = this._startRequest(
      timeoutMs ?? DEFAULT_MULTIPART_TIMEOUT_MS,
      signal
    );

    let response;
    try {
      response = await fetch(url, {
        method: "POST",
        headers,
        body: formData,
        signal: controller.signal,
      });
    } catch (networkErr) {
      throw new ApiError(
        this._networkErrorMessage(
          networkErr,
          controller.signal.reason === "timeout"
        ),
        0,
        null
      );
    } finally {
      cleanup();
    }

    if (!response.ok) {
      let detail = `Erreur HTTP ${response.status} sur ${path}`;
      try {
        const errPayload = await response.json();
        detail = errPayload.detail || detail;
      } catch {
        /* réponse non-JSON, on garde le message générique */
      }
      throw new ApiError(detail, response.status, detail);
    }

    return expectBlob ? response.blob() : response.json();
  }

  /**
   * Prépare un AbortController couplant un timeout interne et un éventuel
   * signal fourni par l'appelant (annulation externe : bouton « Stop », etc.).
   * Les deux relais partagent le même contrôleur ; la cause « timeout » est
   * marquée dans `signal.reason` pour produire un message d'erreur distinct.
   */
  _startRequest(timeoutMs, signal) {
    const controller = new AbortController();
    const timeout = timeoutMs ?? DEFAULT_TIMEOUT_MS;
    let timer = null;
    if (timeout > 0) {
      timer = window.setTimeout(() => controller.abort("timeout"), timeout);
    }
    const onExternalAbort = () => controller.abort();
    if (signal) {
      if (signal.aborted) controller.abort();
      else signal.addEventListener("abort", onExternalAbort);
    }
    const cleanup = () => {
      if (timer !== null) window.clearTimeout(timer);
      signal?.removeEventListener("abort", onExternalAbort);
    };
    return { controller, cleanup };
  }

  _networkErrorMessage(err, isTimeout) {
    if (isTimeout) {
      return `La requête a dépassé le délai autorisé (annulée). Réessayez.`;
    }
    if (err && err.name === "AbortError") {
      return `La requête a été annulée.`;
    }
    return `Impossible de joindre l'API à ${this.baseUrl} (${err.message}). ` +
      `Vérifiez l'URL, que le serveur tourne, et la config CORS.`;
  }
}