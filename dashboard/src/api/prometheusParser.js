/**
 * prometheusParser.js
 * ---------------------------------------------------------------------
 * Parseur du format texte Prometheus (l'endpoint `GET /metrics`) et
 * normalisation de l'endpoint JSON de secours (`GET /metrics/json`).
 *
 * Le dashboard le consomme sans dépendre de Grafana ni d'un Prometheus
 * externe : on extrait directement les compteurs et histogrammes.
 *
 * À partir de n'importe quelle source (texte ou JSON proxy), on produit un
 * « snapshot » canonique :
 *
 *   {
 *     scrapedAtMs: <timestamp>,
 *     counters:   [{ name, labels: {method,path,status_code}, value }],
 *     histograms:[{ name, labels: {method,path,status_code}, count, sum }]
 *   }
 *
 * Puis `aggregateSnapshot()` transforme ce snapshot cumulatif en agrégats
 * faciles à afficher (total requêtes, par statut, par path, latence…).
 */

const MONITORING_PATHS = new Set(["/metrics", "/metrics/json", "/health"]);

/** True si le path provient du polling du dashboard lui-même (à exclure). */
function isMonitoringPath(path) {
  if (!path) return false;
  const clean = String(path).split("?")[0];
  return MONITORING_PATHS.has(clean);
}

/** Parse la section labels `{k="v",k2="v2"}` d'une ligne d'échantillon. */
function parseLabels(labelsStr) {
  const labels = {};
  const re = /(\w+)\s*=\s*"((?:\\.|[^"\\])*)"/g;
  let m;
  while ((m = re.exec(labelsStr)) !== null) {
    labels[m[1]] = m[2].replace(/\\"/g, '"').replace(/\\\\/g, "\\");
  }
  return labels;
}

/**
 * Parse le corps `/metrics` (format texte Prometheus).
 * @returns {Object} snapshot canonique (cf. en-tête de fichier).
 */
export function parsePrometheusText(text) {
  const counters = [];
  const histSum = {}; // key -> sum
  const histCount = {}; // key -> count
  const histOrder = [];
  const seenTypes = {};

  const lines = String(text ?? "").split(/\r?\n/);

  lines.forEach((raw) => {
    const line = raw.trim();
    if (!line) return;

    // Déclarations HELP / TYPE.
    if (line.startsWith("#")) {
      const rest = line.slice(1).trim();
      const m = /^TYPE\s+(\S+)\s+(\w+)/.exec(rest);
      if (m) seenTypes[m[1]] = m[2];
      return;
    }

    // Ligne d'échantillon : name{labels} value  (ou  name value).
    const withLabels = /^([A-Za-z_:][A-Za-z0-9_:]*)\{([^}]*)\}\s+([^ ]+)$/.exec(line);
    const noLabels = /^([A-Za-z_:][A-Za-z0-9_:]*)\s+([^ ]+)$/.exec(line);
    if (!withLabels && !noLabels) return;

    const name = withLabels ? withLabels[1] : noLabels[1];
    const labels = withLabels ? parseLabels(withLabels[2]) : {};
    const value = parseFloat(withLabels ? withLabels[3] : noLabels[2]);
    if (Number.isNaN(value)) return;

    const type = seenTypes[name] || seenTypes[name.replace(/(_sum|_count|_bucket)$/, "")] || "";

    // Histogramme : les samples _sum / _count (+ le=...) décrivent une famille.
    if (type === "histogram" || /_(sum|count|bucket)$/.test(name)) {
      const baseName = name.replace(/(_sum|_count|_bucket)$/, "");
      const hasBucket = labels.le !== undefined;
      const fmtLabels = { ...labels };
      delete fmtLabels.le;
      const key = `${baseName}\u0000${JSON.stringify(fmtLabels)}`;

      if (name.endsWith("_sum")) {
        if (!(key in histSum)) {
          histSum[key] = { name: baseName, labels: fmtLabels, sum: 0, count: 0 };
          histOrder.push(key);
        }
        histSum[key].sum = value;
      } else if (name.endsWith("_count") && !hasBucket) {
        if (!(key in histCount)) {
          histCount[key] = { name: baseName, labels: fmtLabels, count: 0 };
        }
        histCount[key].count = value;
      }
      // On ignore les buckets intermédiaires : seuls _sum + _count sont utiles.
      return;
    }

    // Compteur simple / gauge.
    counters.push({ name, labels, value });
  });

  histOrder.forEach((key) => {
    const countEntry = histCount[key];
    histSum[key].count = countEntry ? countEntry.count : 0;
  });

  return {
    scrapedAtMs: Date.now(),
    counters,
    histograms: histOrder.map((k) => histSum[k]),
  };
}

/**
 * Normalise la réponse de `GET /metrics/json` (proxy JSON) vers le même
 * snapshot canonique que parsePrometheusText.
 */
export function normalizeJsonProxy(json) {
  const payload = json || {};
  const counters = (payload.counters || []).map((c) => ({
    name: c.name,
    labels: c.labels || {},
    value: typeof c.value === "number" ? c.value : parseFloat(c.value) || 0,
  }));
  const histograms = (payload.histograms || []).map((h) => ({
    name: h.name,
    labels: h.labels || {},
    count: typeof h.count === "number" ? h.count : parseInt(h.count, 10) || 0,
    sum: typeof h.sum === "number" ? h.sum : parseFloat(h.sum) || 0,
  }));
  return {
    scrapedAtMs: payload.scrape_at_ms || Date.now(),
    counters,
    histograms,
  };
}
/** Clé canonicale "METHOD PATH" pour agréger compteurs / latences. */
function pathKey(method, path) {
  return `${method || ""} ${path || ""}`.trim();
}

/**
 * Transforme un snapshot CUMULATIF en agrégats lisibles par l'UI.
 * (Les valeurs restent des accumulateurs Prometheus : le calcul de débits
 *  à proprement parler se fait par différence entre deux pollings.)
 */
export function aggregateSnapshot(snapshot) {
  const counters = snapshot?.counters || [];
  const histograms = snapshot?.histograms || [];

  let requestsTotal = 0;
  const requestsByStatus = {}; // status -> count
  const requestsByPathMap = {}; // key -> {method, path, count}

  for (const c of counters) {
    if (c.name !== "http_requests_total") continue;
    const { method, path, status_code } = c.labels;
    if (isMonitoringPath(path)) continue;

    requestsTotal += c.value;
    const status = String(status_code ?? "none");
    requestsByStatus[status] = (requestsByStatus[status] || 0) + c.value;

    const key = pathKey(method, path);
    const e = requestsByPathMap[key] || (requestsByPathMap[key] = { method, path, count: 0 });
    e.count += c.value;
  }

  const latencyByPathMap = {}; // key -> {method, path, count, sum}
  let latencyTotalCount = 0;
  let latencyTotalSum = 0;

  for (const h of histograms) {
    if (h.name !== "http_request_duration_seconds") continue;
    const { method, path } = h.labels;
    if (isMonitoringPath(path)) continue;

    latencyTotalCount += h.count;
    latencyTotalSum += h.sum;

    const key = pathKey(method, path);
    const e = latencyByPathMap[key] || (latencyByPathMap[key] = { method, path, count: 0, sum: 0 });
    e.count += h.count;
    e.sum += h.sum;
  }

  const requestsByPath = Object.values(requestsByPathMap).sort((a, b) => b.count - a.count);
  const latencyByPath = Object.values(latencyByPathMap).sort((a, b) => b.sum - a.sum);

  return {
    scrapedAtMs: snapshot?.scrapedAtMs || Date.now(),
    requestsTotal,
    requestsByStatus: Object.entries(requestsByStatus)
      .sort(([a], [b]) => (Number(a) || 0) - (Number(b) || 0))
      .map(([status, count]) => ({ status, count })),
    requestsByPath,
    latencyTotal: { count: latencyTotalCount, sum: latencyTotalSum },
    latencyByPath,
  };
}

/** Extract latence moyenne (ms) pour un (method, path) depuis une liste. */
function findLatency(arr, method, path) {
  const item = (arr || []).find(
    (p) => (!method || p.method === method) && p.path === path
  );
  if (!item || !item.count) return { count: 0, sum: 0, meanMs: 0 };
  return { count: item.count, sum: item.sum, meanMs: (item.sum / item.count) * 1000 };
}

/**
 * Calcule la différence entre deux snapshots cumulatifs (récent - précédent).
 * Retourne les variations par entité (deltas), ou null si invalide.
 * Utilisé pour dériver des taux (requêtes/min, latence) d'un intervalle.
 */
export function deltaSnapshots(prev, next) {
  if (!prev || !next) return null;

  const byKey = (arr, getKey) => {
    const map = new Map();
    (arr || []).forEach((item) => map.set(getKey(item), item));
    return map;
  };

  // --- Compteurs requêtes ---
  const prevReq = byKey(prev.requestsByPath, (p) => `${p.method} ${p.path}`);
  const nextReq = byKey(next.requestsByPath, (p) => `${p.method} ${p.path}`);

  const paths = new Set([...prevReq.keys(), ...nextReq.keys()]);
  const requestDeltas = [];
  let requestsDeltaTotal = 0;
  for (const key of paths) {
    const before = prevReq.get(key)?.count || 0;
    const after = nextReq.get(key)?.count || 0;
    const d = Math.max(0, after - before);
    requestsDeltaTotal += d;
    const nextItem = nextReq.get(key);
    requestDeltas.push({
      method: nextItem?.method || prevReq.get(key)?.method || "",
      path: nextItem?.path || prevReq.get(key)?.path || "",
      delta: d,
    });
  }

  // --- Statuts ---
  const prevStatus = new Map(prev.requestsByStatus.map((s) => [s.status, s.count]));
  const statusDeltas = [];
  const allStatuses = new Set([
    ...prev.requestsByStatus.map((s) => s.status),
    ...next.requestsByStatus.map((s) => s.status),
  ]);
  for (const status of allStatuses) {
    const after = next.requestsByStatus.find((s) => s.status === status)?.count || 0;
    const d = Math.max(0, after - (prevStatus.get(status) || 0));
    if (d > 0) statusDeltas.push({ status, delta: d });
  }

  // --- Latence ---
  const prevLat = byKey(prev.latencyByPath, (p) => `${p.method} ${p.path}`);
  const nextLat = byKey(next.latencyByPath, (p) => `${p.method} ${p.path}`);
  const latencyPaths = new Set([...prevLat.keys(), ...nextLat.keys()]);

  const latencyDeltas = [];
  let latencyDeltaSum = 0;
  let latencyDeltaCount = 0;
  for (const key of latencyPaths) {
    const before = prevLat.get(key);
    const after = nextLat.get(key);
    const dc = Math.max(0, (after?.count || 0) - (before?.count || 0));
    const ds = Math.max(0, (after?.sum || 0) - (before?.sum || 0));
    latencyDeltaSum += ds;
    latencyDeltaCount += dc;
    if (dc > 0 || ds > 0) {
      const nextItem = nextLat.get(key);
      latencyDeltas.push({
        method: nextItem?.method || before?.method || "",
        path: nextItem?.path || before?.path || "",
        count: dc,
        sum: ds,
      });
    }
  }

  const elapsedMs = next.scrapedAtMs - prev.scrapedAtMs;
  return {
    elapsedMs: elapsedMs > 0 ? elapsedMs : 0,
    requestsTotal: requestsDeltaTotal,
    requestsPerMin: elapsedMs > 0 ? (requestsDeltaTotal / elapsedMs) * 60000 : 0,
    requestsByPath: requestDeltas,
    requestsByStatus: statusDeltas,
    latencyTotal: { count: latencyDeltaCount, sum: latencyDeltaSum },
    latencyPredict: findLatency(latencyDeltas, "POST", "/predict"),
    latencyByPath: latencyDeltas,
  };
}

export default { parsePrometheusText, normalizeJsonProxy, aggregateSnapshot, deltaSnapshots };