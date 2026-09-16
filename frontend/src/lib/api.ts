/* API client.
 *
 * The auth token lives in localStorage and travels in the Authorization
 * header. It is never placed in a cookie, so the browser never attaches it to
 * a cross-site request and there is no CSRF surface to defend.
 */

const TOKEN_KEY = 'emi.token';

export function getToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setToken(token: string): void {
  try {
    localStorage.setItem(TOKEN_KEY, token);
  } catch {
    /* private mode: the session still works, it just will not be remembered */
  }
}

export function clearToken(): void {
  try {
    localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* ignore */
  }
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = getToken();
  const headers = new Headers(init.headers);
  headers.set('Content-Type', 'application/json');
  if (token) headers.set('Authorization', `Bearer ${token}`);

  const response = await fetch(`/api${path}`, { ...init, headers });
  if (response.status === 401) {
    clearToken();
    throw new ApiError(401, 'Not signed in');
  }
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* keep statusText */
    }
    throw new ApiError(response.status, detail);
  }
  return (await response.json()) as T;
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) }),
  put: <T>(path: string, body: unknown) =>
    request<T>(path, { method: 'PUT', body: JSON.stringify(body) }),
  del: <T>(path: string) => request<T>(path, { method: 'DELETE' }),
};

/* --- types ------------------------------------------------------------- */

export type Confidence = 'HIGH' | 'MEDIUM' | 'LOW';

export interface TickerLink {
  ticker: string;
  confidence: Confidence;
  matched_alias: string | null;
  source: string;
  user_corrected: boolean;
}

export interface Analysis {
  status: string;
  model: string | null;
  event_type: string | null;
  sentiment: number | null;
  market_impact: number | null;
  confidence: number | null;
  time_horizon: string | null;
  reasoning: string | null;
  facts: string[];
  stated_positions: string[];
  third_party_claims: string[];
  speculation: string[];
  uncertainty: string[];
  validation_error: string | null;
}

export interface HorizonStat {
  horizon: string;
  n: number;
  mean: number | null;
  median: number | null;
  stdev: number | null;
  positive_pct: number | null;
  negative_pct: number | null;
  min: number | null;
  max: number | null;
  p5: number | null;
  p95: number | null;
  mean_abnormal: number | null;
  median_abnormal: number | null;
  flag: 'ok' | 'limited' | 'unreliable';
  basis: string;
}

export interface Signal {
  id: string;
  ticker: string;
  horizon: string;
  score: number;
  label: string;
  confidence: number;
  sample_size: number;
  sample_flag: 'ok' | 'limited' | 'unreliable';
  components: Record<string, unknown>;
  weights: Record<string, number>;
  uncertainties: string[];
  historical_stats: {
    ticker?: string;
    event_type?: string;
    match_basis?: string;
    n_matches?: number;
    stats?: Record<string, HorizonStat>;
    matches?: Array<Record<string, unknown>>;
  };
  created_at: string;
}

export interface EventItem {
  id: string;
  source_key: string;
  author: string | null;
  title: string | null;
  text: string;
  excerpt: string;
  url: string | null;
  event_type: string;
  source_timestamp: string;
  ingestion_timestamp: string;
  processing_timestamp: string | null;
  relevance_score: number;
  relevant: boolean;
  analysis_status: string;
  is_historical: boolean;
  tickers: TickerLink[];
  analysis: Analysis | null;
  signals: Signal[];
  alert_triggered: boolean;
}

export interface AppConfig {
  app_name: string;
  environment: string;
  web_push_public_key: string | null;
  push_configured: boolean;
  email_configured: boolean;
  llm_configured: boolean;
  llm_mode: string;
  market_provider: string;
  supports_intraday: boolean;
  embedding_provider: string;
  embedding_semantic: boolean;
  embedding_label: string;
  signal_weights: Record<string, number>;
  signal_thresholds: Record<string, number>;
  signal_return_scale: number;
  sample_size_gates: { unreliable_below: number; limited_below: number };
  primary_horizon: string;
  phase: number;
}

export interface NotificationItem {
  id: string;
  notification_type: string;
  severity: string;
  title: string;
  body: string;
  event_id: string | null;
  ticker: string | null;
  created_at: string;
  read_at: string | null;
  archived_at: string | null;
  payload: Record<string, unknown>;
  deliveries: Array<{ channel: string; status: string; provider_response: string | null }>;
}
