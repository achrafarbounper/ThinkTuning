/**
 * clientCore.ts
 * ---------------------------------------------------------------------
 * Cœur HTTP minimal et « critique » du client API ThinkTuning.
 *
 * C'est la SEULE partie indispensable au premier rendu : toute l'application
 * consomme ce transport (fetch) dès le montage (via AppProvider). Le reste du
 * client (endpoints métier) vit dans sentimentApiClient.ts, et les helpers
 * agent + étapes de jobs dans des modules dédiés : cela maintient petit le
 * bundle initial chargé sur le chemin critique.
 */

export const DEFAULT_BASE_URL: string =
  import.meta.env.VITE_API_URL ?? "http://localhost:8000";

// --- Timeout réseau -----------------------------------------------------------

/** Délai par défaut d'une requête JSON (30 s). */
export const DEFAULT_TIMEOUT_MS = 30_000;
/** Délai par défaut d'un upload multipart (60 s, fichiers CSV potentiellement lourds). */
export const DEFAULT_MULTIPART_TIMEOUT_MS = 60_000;

export interface ApiConfig {
  baseUrl?: string;
  apiKey?: string;
}

export type QueryParams = Record<
  string,
  string | number | boolean | undefined | null
>;

export class ApiError extends Error {
  status: number;
  detail: unknown;

  constructor(message: string, status: number, detail: unknown = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

export interface RequestOptions {
  method?: string;
  body?: unknown;
  query?: QueryParams;
  timeoutMs?: number;
  signal?: AbortSignal;
}

interface MultipartOptions {
  formData: FormData;
  query?: QueryParams;
  expectBlob?: boolean;
  timeoutMs?: number;
  signal?: AbortSignal;
}

/**
 * Classe « natte » du transport HTTP : en-têtes, construction d'URL, requêtes
 * JSON et multipart avec timeout + support d'annulation externe (AbortSignal).
 * Les endpoints métier l'étendent (voir sentimentApiClient.ts).
 */
export class SentimentApiClientCore {
  baseUrl: string;
  apiKey: string;

  constructor({ baseUrl = DEFAULT_BASE_URL, apiKey = "" }: ApiConfig = {}) {
    this.baseUrl = baseUrl.replace(/\/+$/, "");
    this.apiKey = apiKey || "";
  }

  setConfig({ baseUrl, apiKey }: ApiConfig = {}): void {
    if (baseUrl !== undefined) this.baseUrl = baseUrl.replace(/\/+$/, "");
    if (apiKey !== undefined) this.apiKey = apiKey;
  }

  _headers(isJson: boolean): Record<string, string> {
    const headers: Record<string, string> = {};
    if (isJson) headers["Content-Type"] = "application/json";
    if (this.apiKey) headers["X-API-Key"] = this.apiKey;
    return headers;
  }

  _buildUrl(path: string, query?: QueryParams): string {
    let url = `${this.baseUrl}${path}`;
    if (query) {
      const entries = Object.entries(query).filter(
        ([, v]) => v !== undefined && v !== null && v !== ""
      ) as [string, string][];
      if (entries.length) {
        url += `?${new URLSearchParams(entries).toString()}`;
      }
    }
    return url;
  }
  /**
   * Transport partagé : applique timeout + annulation externe (AbortSignal
   * couplés sur un seul controller) puis fetch. Toute requête du client
   * passe par ici — y compris /metrics qui, via un fetch brut, échappait
   * au timeout, à la clé API et à la normalisation des erreurs.
   */
  async _send(url: string, init: RequestInit, timeoutMs?: number, signal?: AbortSignal): Promise<Response> {
    const { controller, cleanup } = this._startRequest(timeoutMs, signal);
    init.signal = controller.signal;
    try {
      return await fetch(url, init);
    } catch (networkErr) {
      throw new ApiError(
        this._networkErrorMessage(
          networkErr instanceof Error ? networkErr : new Error(String(networkErr)),
          controller.signal.reason === "timeout"
        ),
        0,
        null
      );
    } finally {
      cleanup();
    }
  }

  async _request<T = unknown>(
    path: string,
    options: RequestOptions = {}
  ): Promise<T | null> {
    const { method = "GET", body, query, timeoutMs, signal } = options;
    const url = this._buildUrl(path, query);
    const init: RequestInit = { method, headers: this._headers(body !== undefined) };
    if (body !== undefined) init.body = JSON.stringify(body);

    const response = await this._send(url, init, timeoutMs, signal);

    if (response.status === 204) return null;

    const contentType = response.headers.get("content-type") || "";
    const isJson = contentType.includes("application/json");
    const payload: unknown = isJson
      ? await response.json().catch(() => null)
      : await response.text();

    if (!response.ok) {
      const detail = isJson && payload ? (payload as { detail?: unknown }).detail : payload;
      throw new ApiError(
        typeof detail === "string" && detail
          ? detail
          : `Erreur HTTP ${response.status} sur ${path}`,
        response.status,
        detail
      );
    }

    return payload as T;
  }

  /**
   * Requête renvoyant le corps TEXTUEL brut (ex. exposition Prometheus de
   * /metrics). Même transport que _request : timeout, X-API-Key, erreurs
   * ApiError normalisées.
   */
  async _requestText(
    path: string,
    options: RequestOptions = {}
  ): Promise<string> {
    const { method = "GET", query, timeoutMs, signal } = options;
    const url = this._buildUrl(path, query);
    const init: RequestInit = { method, headers: this._headers(false) };

    const response = await this._send(url, init, timeoutMs, signal);

    if (!response.ok) {
      const payload: unknown = await response.json().catch(() => null);
      // Le détail FastAPI vit sous la clé "detail" (comme dans _request).
      const detail =
        payload !== null && typeof payload === "object" && "detail" in payload
          ? (payload as { detail?: unknown }).detail
          : payload;
      throw new ApiError(
        typeof detail === "string" && detail
          ? detail
          : `Erreur HTTP ${response.status} sur ${path}`,
        response.status,
        detail
      );
    }
    return response.text();
  }

  async _requestMultipart<T = unknown>(
    path: string,
    { formData, query, expectBlob = false, timeoutMs, signal }: MultipartOptions
  ): Promise<T> {
    const url = this._buildUrl(path, query);
    const headers: Record<string, string> = this.apiKey
      ? { "X-API-Key": this.apiKey }
      : {};

    const response = await this._send(
      url,
      { method: "POST", headers, body: formData },
      timeoutMs ?? DEFAULT_MULTIPART_TIMEOUT_MS,
      signal
    );

    if (!response.ok) {
      let detail: unknown = `Erreur HTTP ${response.status} sur ${path}`;
      try {
        const errPayload: { detail?: unknown } = await response.json();
        detail = errPayload.detail ?? detail;
      } catch {
        /* réponse non-JSON, on garde le message générique */
      }
      throw new ApiError(
        typeof detail === "string" ? detail : `Erreur HTTP ${response.status} sur ${path}`,
        response.status,
        detail
      );
    }

    return (expectBlob ? response.blob() : response.json()) as Promise<T>;
  }

  /**
   * Prépare un AbortController couplant un timeout interne et un éventuel
   * signal fourni par l'appelant (annulation externe : bouton « Stop », etc.).
   * Les deux relais partagent le même contrôleur ; la cause « timeout » est
   * marquée dans `signal.reason` pour produire un message d'erreur distinct.
   */
  _startRequest(
    timeoutMs: number | undefined,
    signal?: AbortSignal
  ): { controller: AbortController; cleanup: () => void } {
    const controller = new AbortController();
    const timeout = timeoutMs ?? DEFAULT_TIMEOUT_MS;
    let timer: number | null = null;
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

  _networkErrorMessage(err: Error, isTimeout: boolean): string {
    if (isTimeout) {
      return `La requête a dépassé le délai autorisé (annulée). Réessayez.`;
    }
    if (err.name === "AbortError") {
      return `La requête a été annulée.`;
    }
    return `Impossible de joindre l'API à ${this.baseUrl} (${err.message}). ` +
      `Vérifiez l'URL, que le serveur tourne, et la config CORS.`;
  }
}
