import { FormEvent, useState } from 'react';
import { Link } from 'react-router-dom';

import { Disclaimer, Empty, ErrorBox, Section, Spinner } from '../components/ui';
import { api } from '../lib/api';
import { labelColour, localTime, signed } from '../lib/format';
import { useApi } from '../lib/useApi';

interface WatchlistBody {
  name: string;
  items: Array<{
    ticker: string;
    name: string;
    notes: string | null;
    latest_signal: {
      score: number;
      label: string;
      confidence: number;
      sample_size: number;
      sample_flag: string;
      created_at: string;
    } | null;
  }>;
}

interface Rule {
  id: string;
  name: string;
  enabled: boolean;
  rule_type: string;
  tickers: string[];
  keywords: string[];
  bullish_threshold: number | null;
  bearish_threshold: number | null;
  min_confidence: number;
  min_sample_size: number;
  channels: string[];
}

export default function Watchlist() {
  const watchlist = useApi<WatchlistBody>('/watchlist');
  const rules = useApi<{ items: Rule[] }>('/alert-rules');
  const [symbol, setSymbol] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function add(event: FormEvent) {
    event.preventDefault();
    if (!symbol.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await api.post('/watchlist', { ticker: symbol });
      setSymbol('');
      watchlist.reload();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function remove(ticker: string) {
    await api.del(`/watchlist/${ticker}`);
    watchlist.reload();
  }

  async function toggleRule(rule: Rule) {
    await api.put(`/alert-rules/${rule.id}`, { ...rule, enabled: !rule.enabled });
    rules.reload();
  }

  return (
    <div>
      <Section title="Watchlist">
        <form onSubmit={add} className="mb-3 flex gap-2">
          <input
            className="input"
            value={symbol}
            onChange={(event) => setSymbol(event.target.value)}
            placeholder="Add a ticker, e.g. MSFT"
            aria-label="Ticker symbol"
            autoCapitalize="characters"
          />
          <button className="btn-primary shrink-0" disabled={busy}>
            Add
          </button>
        </form>
        {error && <p className="mb-2 text-sm text-bear">{error}</p>}

        {watchlist.loading && <Spinner />}
        {watchlist.error && <ErrorBox message={watchlist.error} onRetry={watchlist.reload} />}
        {watchlist.data?.items.length === 0 && <Empty>Your watchlist is empty.</Empty>}

        <ul className="space-y-2">
          {watchlist.data?.items.map((row) => (
            <li key={row.ticker} className="card flex items-center justify-between gap-3">
              <div className="min-w-0">
                <Link to={`/ticker/${row.ticker}`} className="font-semibold text-accent">
                  {row.ticker}
                </Link>
                <div className="truncate text-xs text-muted">{row.name}</div>
                {row.latest_signal && (
                  <div className="mt-1 text-xs">
                    <span className={labelColour(row.latest_signal.label)}>
                      {row.latest_signal.label} {signed(row.latest_signal.score)}
                    </span>
                    <span className="text-muted">
                      {' '}
                      · N={row.latest_signal.sample_size} ({row.latest_signal.sample_flag}) ·{' '}
                      {localTime(row.latest_signal.created_at)}
                    </span>
                  </div>
                )}
              </div>
              <button
                className="btn shrink-0 text-xs"
                onClick={() => remove(row.ticker)}
                aria-label={`Remove ${row.ticker}`}
              >
                Remove
              </button>
            </li>
          ))}
        </ul>
      </Section>

      <Section title="Alert rules">
        {rules.loading && <Spinner />}
        {rules.data?.items.length === 0 && <Empty>No alert rules configured.</Empty>}
        <ul className="space-y-2">
          {rules.data?.items.map((rule) => (
            <li key={rule.id} className="card">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="font-medium">{rule.name}</div>
                  <div className="mt-1 text-xs text-muted">
                    {rule.rule_type.replace('_', ' ')}
                    {rule.tickers.length > 0 && ` · ${rule.tickers.join(', ')}`}
                    {rule.keywords.length > 0 && ` · "${rule.keywords.join('", "')}"`}
                  </div>
                  {rule.rule_type === 'signal_threshold' && (
                    <div className="mt-1 text-xs text-muted">
                      thresholds {signed(rule.bullish_threshold)} / {signed(rule.bearish_threshold)}{' '}
                      · min confidence {rule.min_confidence} · min N {rule.min_sample_size}
                    </div>
                  )}
                  <div className="mt-1 text-[11px] text-muted">
                    channels: {rule.channels.join(', ')}
                  </div>
                </div>
                <button
                  className={`btn shrink-0 text-xs ${rule.enabled ? '' : 'opacity-60'}`}
                  onClick={() => toggleRule(rule)}
                >
                  {rule.enabled ? 'On' : 'Off'}
                </button>
              </div>
            </li>
          ))}
        </ul>
        <p className="mt-2 text-[11px] text-muted">
          Threshold alerts fire when a signal crosses the boundary, then re-arm once it returns
          inside it — so a signal sitting above the line does not alert repeatedly.
        </p>
      </Section>

      <Disclaimer />
    </div>
  );
}
