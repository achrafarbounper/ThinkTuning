/**
 * prometheusParser.ts
 * ---------------------------------------------------------------------
 * Parseur du format texte Prometheus (l'endpoint GET /metrics) et
 * normalisation de l'endpoint JSON de secours (GET /metrics/json).
 */

export interface PrometheusLabels {
  [key: string]: string;
}

export interface PrometheusCounter {
  name: string;
  labels: PrometheusLabels;
  value: number;
}

export interface PrometheusHistogram {
  name: string;
  labels: PrometheusLabels;
  count: number;
  sum: number;
}

export interface PrometheusSnapshot {
  scrapedAtMs: number;
  counters: PrometheusCounter[];
  histograms: PrometheusHistogram[];
}

export interface RequestsByPath {
  method: string;
  path: string;
  count: number;
  sum: number;
}

export interface RequestsByStatus {
  status: string;
  count: number;
}

export interface AggregatedSnapshot {
  scrapedAtMs: number;
  totalRequests: number;
  requestsByPath: RequestsByPath[];
  requestsByStatus: RequestsByStatus[];
  latencyByPath: RequestsByPath[];
  latencyPredict: { count: number; sum: number; meanMs: number };
}

export interface SnapshotDelta {
  elapsedMs: number;
  requestsTotal: number;
  requestsPerMin: number;
  requestsByPath: Array<{ method: string; path: string; delta: number }>;
  requestsByStatus: Array<{ status: string; delta: number }>;
  latencyTotal: { count: number; sum: number };
  latencyPredict: { count: number; sum: number; meanMs: number };
  latencyByPath: Array<{ method: string; path: string; count: number; sum: number }>;
}

const MONITORING_PATHS = new Set(['/metrics', '/metrics/json', '/health']);

function isMonitoringPath(path: string | undefined | null): boolean {
  if (!path) return false;
  const clean = String(path).split('?')[0];
  return MONITORING_PATHS.has(clean);
}

function parseLabels(labelsStr: string): PrometheusLabels {
  const out: PrometheusLabels = {};
  if (!labelsStr) return out;
  let i = 0;
  while (i < labelsStr.length) {
    while (i < labelsStr.length && labelsStr[i] === ',') i++;
    const eq = labelsStr.indexOf('=', i);
    if (eq === -1) break;
    const key = labelsStr.slice(i, eq).trim();
    if (!key) break;
    const quoteStart = eq + 1;
    if (labelsStr[quoteStart] !== '"') break;
    let j = quoteStart + 1;
    let val = '';
    while (j < labelsStr.length) {
      if (labelsStr[j] === '\\' && j + 1 < labelsStr.length) {
        val += labelsStr[j + 1];
        j += 2;
        continue;
      }
      if (labelsStr[j] === '"') break;
      val += labelsStr[j];
      j++;
    }
    out[key] = val;
    i = j + 1;
  }
  return out;
}

export function parsePrometheusText(text: string | undefined | null): PrometheusSnapshot {
  const counters: PrometheusCounter[] = [];
  const histAgg: Record<string, PrometheusHistogram> = {};
  const histOrder: string[] = [];
  const seenTypes: Record<string, string> = {};

  const lines = String(text ?? '').split(/\r?\n/);

  lines.forEach((raw) => {
    const line = raw.trim();
    if (!line) return;

    if (line.startsWith('#')) {
      const rest = line.slice(1).trim();
      const m = /^TYPE\s+(\S+)\s+(\w+)/.exec(rest);
      if (m) seenTypes[m[1]] = m[2];
      return;
    }

    const withLabels = /^([A-Za-z_:][A-Za-z0-9_:]*)\{([^}]*)\}\s+([^ ]+)$/.exec(line);
    const noLabels = /^([A-Za-z_:][A-Za-z0-9_:]*)\s+([^ ]+)$/.exec(line);
    if (!withLabels && !noLabels) return;

    const name = withLabels ? withLabels[1] : noLabels![1];
    const labels = withLabels ? parseLabels(withLabels[2]) : {};
    const value = parseFloat(withLabels ? withLabels[3] : noLabels![2]);
    if (Number.isNaN(value)) return;

    const type = seenTypes[name] || seenTypes[name.replace(/(_sum|_count|_bucket)$/, '')] || '';

    if (type === 'histogram' || /_(sum|count|bucket)$/.test(name)) {
      const baseName = name.replace(/_(sum|count|bucket)$/, '');
      const isBucket = name.endsWith('_bucket');
      if (!isBucket) {
        if (!histAgg[baseName]) {
          histAgg[baseName] = { name: baseName, labels, count: 0, sum: 0 };
          histOrder.push(baseName);
        }
        if (name.endsWith('_count')) histAgg[baseName].count += value;
        if (name.endsWith('_sum')) histAgg[baseName].sum += value;
      }
    } else {
      counters.push({ name, labels, value });
    }
  });

  const histograms = histOrder.map((k) => histAgg[k]);
  return { scrapedAtMs: Date.now(), counters, histograms };
}

export function aggregateSnapshot(snap: PrometheusSnapshot | null | undefined): AggregatedSnapshot | null {
  if (!snap) return null;

  const counters = snap.counters || [];
  const histograms = snap.histograms || [];

  const byPathMap = new Map<string, RequestsByPath>();
  let totalRequests = 0;

  for (const c of counters) {
    if (c.name !== 'http_requests_total') continue;
    const { method = '', path: p = '' } = c.labels;
    if (isMonitoringPath(p)) continue;
    const key = `${method} ${p}`;
    const existing = byPathMap.get(key);
    if (existing) {
      existing.count += c.value;
    } else {
      byPathMap.set(key, { method, path: p, count: c.value, sum: 0 });
    }
    totalRequests += c.value;
  }

  const requestsByPath = Array.from(byPathMap.values());

  const byStatusMap = new Map<string, number>();
  for (const c of counters) {
    if (c.name !== 'http_requests_total') continue;
    const status = c.labels.status_code ?? '';
    byStatusMap.set(status, (byStatusMap.get(status) || 0) + c.value);
  }
  const requestsByStatus: RequestsByStatus[] = Array.from(byStatusMap.entries()).map(
    ([status, count]) => ({ status, count })
  );

  const latMap = new Map<string, RequestsByPath>();
  for (const h of histograms) {
    if (h.name !== 'request_latency_seconds') continue;
    const { method = '', path: p = '' } = h.labels || {};
    if (isMonitoringPath(p)) continue;
    const key = `${method} ${p}`;
    const existing = latMap.get(key);
    if (existing) {
      existing.count += h.count;
      existing.sum += h.sum;
    } else {
      latMap.set(key, { method, path: p, count: h.count, sum: h.sum });
    }
  }
  const latencyByPath = Array.from(latMap.values());
  const latencyPredict = findLatency(latencyByPath, 'POST', '/predict');

  return {
    scrapedAtMs: snap.scrapedAtMs,
    totalRequests,
    requestsByPath,
    requestsByStatus,
    latencyByPath,
    latencyPredict,
  };
}

function findLatency(arr: RequestsByPath[], method: string, path: string): { count: number; sum: number; meanMs: number } {
  const item = arr.find((p) => (!method || p.method === method) && p.path === path);
  if (!item || !item.count) return { count: 0, sum: 0, meanMs: 0 };
  return { count: item.count, sum: item.sum, meanMs: (item.sum / item.count) * 1000 };
}

export function computeDelta(prev: AggregatedSnapshot | null, curr: AggregatedSnapshot | null): SnapshotDelta | null {
  if (!prev || !curr) return null;

  const elapsedMs = Math.max(0, curr.scrapedAtMs - prev.scrapedAtMs);
  const requestsTotal = Math.max(0, curr.totalRequests - prev.totalRequests);
  const requestsPerMin = elapsedMs > 0 ? (requestsTotal / elapsedMs) * 60000 : 0;

  const byPath = new Map<string, number>();
  for (const p of prev.requestsByPath) byPath.set(`${p.method} ${p.path}`, p.count);
  const requestsByPath = curr.requestsByPath.map((p) => {
    const prevCount = byPath.get(`${p.method} ${p.path}`) || 0;
    return { method: p.method, path: p.path, delta: Math.max(0, p.count - prevCount) };
  });

  const byStatus = new Map<string, number>();
  for (const s of prev.requestsByStatus) byStatus.set(s.status, s.count);
  const requestsByStatus = curr.requestsByStatus.map((s) => {
    const prevCount = byStatus.get(s.status) || 0;
    return { status: s.status, delta: Math.max(0, s.count - prevCount) };
  });

  const latencyTotal = {
    count: Math.max(0, curr.latencyPredict.count - prev.latencyPredict.count),
    sum: Math.max(0, curr.latencyPredict.sum - prev.latencyPredict.sum),
  };

  const latencyPredict = {
    count: latencyTotal.count,
    sum: latencyTotal.sum,
    meanMs: latencyTotal.count > 0 ? (latencyTotal.sum / latencyTotal.count) * 1000 : 0,
  };

  const latByPath = new Map<string, { count: number; sum: number }>();
  for (const p of prev.latencyByPath) latByPath.set(`${p.method} ${p.path}`, { count: p.count, sum: p.sum });
  const latencyByPath = curr.latencyByPath.map((p) => {
    const prevVal = latByPath.get(`${p.method} ${p.path}`) || { count: 0, sum: 0 };
    return {
      method: p.method,
      path: p.path,
      count: Math.max(0, p.count - prevVal.count),
      sum: Math.max(0, p.sum - prevVal.sum),
    };
  });

  return {
    elapsedMs,
    requestsTotal,
    requestsPerMin,
    requestsByPath,
    requestsByStatus,
    latencyTotal,
    latencyPredict,
    latencyByPath,
  };
}

export function normalizeJsonProxy(json: Record<string, unknown> | null | undefined): PrometheusSnapshot {
  if (!json) return { scrapedAtMs: Date.now(), counters: [], histograms: [] };

  const counters: PrometheusCounter[] = [];
  const histograms: PrometheusHistogram[] = [];

  for (const [key, raw] of Object.entries(json)) {
    if (key === 'http_requests_total' && typeof raw === 'object' && raw !== null) {
      for (const [labelStr, val] of Object.entries(raw as Record<string, unknown>)) {
        if (typeof val !== 'number') continue;
        const labels = parseLabels(labelStr);
        if (isMonitoringPath(labels.path)) continue;
        counters.push({ name: key, labels, value: val });
      }
    } else if (typeof raw === 'object' && raw !== null && 'count' in raw && 'sum' in raw) {
      const obj = raw as { count: unknown; sum: unknown };
      if (typeof obj.count === 'number' && typeof obj.sum === 'number') {
        histograms.push({ name: key, labels: {}, count: obj.count, sum: obj.sum });
      }
    }
  }

  return { scrapedAtMs: Date.now(), counters, histograms };
}
