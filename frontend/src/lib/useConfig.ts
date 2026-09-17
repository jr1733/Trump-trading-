/* Shared app config.
 *
 * `useApi` deliberately has no cache -- a stale price shown as current is worse
 * than a spinner. `/config` is the exception: it is capability flags, not data,
 * it cannot change while the page is open, and several components need it at
 * once. One in-flight promise is shared by every caller.
 */

import { useEffect, useState } from 'react';

import { api, type AppConfig } from './api';

let pending: Promise<AppConfig> | null = null;
let cached: AppConfig | null = null;

function load(): Promise<AppConfig> {
  if (!pending) {
    pending = api
      .get<AppConfig>('/config')
      .then((body) => {
        cached = body;
        return body;
      })
      .catch((err) => {
        // Let the next caller retry rather than caching the failure forever.
        pending = null;
        throw err;
      });
  }
  return pending;
}

export function useConfig(): AppConfig | null {
  const [config, setConfig] = useState<AppConfig | null>(cached);

  useEffect(() => {
    if (cached) return;
    let cancelled = false;
    void load()
      .then((body) => {
        if (!cancelled) setConfig(body);
      })
      .catch(() => {
        /* the banner is advisory; a failed config fetch must not blank a page */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return config;
}

/** True when prices are synthetic rather than from a real market data feed. */
export function marketIsMock(config: AppConfig | null): boolean {
  return config?.market_provider === 'mock';
}

/** True when analyses come from the offline canned scorer, not a model. */
export function analysisIsMock(config: AppConfig | null): boolean {
  return config?.llm_mode === 'canned-mock';
}
