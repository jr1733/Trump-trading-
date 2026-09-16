import { useState } from 'react';
import { Link } from 'react-router-dom';

import EventCard from '../components/EventCard';
import { Disclaimer, Empty, ErrorBox, Section, Spinner, Stat } from '../components/ui';
import type { EventItem, Signal } from '../lib/api';
import { labelColour, num, signed } from '../lib/format';
import { useApi } from '../lib/useApi';

const SORTS = [
  { value: 'newest', label: 'Newest' },
  { value: 'signal', label: 'Signal strength' },
  { value: 'ticker', label: 'Ticker' },
  { value: 'source', label: 'Source' },
  { value: 'event_type', label: 'Event type' },
];

interface Dashboard {
  top_signals: Signal[];
  latest_events: EventItem[];
  market_context: Array<{ symbol: string; close: number | null; change_pct: number | null }>;
  source_health: Array<{
    source: string;
    status: string;
    consecutive_failures: number;
    last_error: string | null;
  }>;
  notification_summary: {
    unread: number;
    alerts_today: number;
    failed_deliveries: number;
    push_status: string;
    next_digest: string;
  };
}

function statusTone(status: string): string {
  if (status === 'ONLINE') return 'text-bull';
  if (status === 'ERROR') return 'text-bear';
  if (status === 'DEGRADED') return 'text-warn';
  return 'text-muted';
}

export default function Live() {
  const [sort, setSort] = useState('newest');
  const [includeFiltered, setIncludeFiltered] = useState(false);

  const dashboard = useApi<Dashboard>('/dashboard');
  const events = useApi<{ total: number; items: EventItem[] }>(
    `/events?limit=40&sort=${sort}&include_filtered=${includeFiltered}`,
  );

  return (
    <div>
      {dashboard.data && (
        <>
          <Section title="Market context">
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
              {dashboard.data.market_context.map((row) => (
                <Stat
                  key={row.symbol}
                  label={row.symbol}
                  value={
                    row.close === null ? (
                      '—'
                    ) : (
                      <span>
                        {num(row.close)}{' '}
                        <span
                          className={
                            (row.change_pct ?? 0) >= 0 ? 'text-bull text-xs' : 'text-bear text-xs'
                          }
                        >
                          {row.change_pct === null ? '' : `${signed(row.change_pct, 2)}%`}
                        </span>
                      </span>
                    )
                  }
                />
              ))}
            </div>
          </Section>

          <Section title="Top signals">
            {dashboard.data.top_signals.length === 0 ? (
              <Empty>No signals yet. Run the worker, or trigger a poll from Settings.</Empty>
            ) : (
              <div className="flex gap-2 overflow-x-auto pb-1">
                {dashboard.data.top_signals.map((signal) => (
                  <Link
                    key={signal.id}
                    to={`/ticker/${signal.ticker}`}
                    className="min-w-[132px] rounded-xl border border-edge bg-panel p-3"
                  >
                    <div className="font-semibold">{signal.ticker}</div>
                    <div className={`text-sm font-bold ${labelColour(signal.label)}`}>
                      {signed(signal.score)}
                    </div>
                    <div className="mt-1 text-[11px] text-muted">
                      {signal.label} · N={signal.sample_size}
                    </div>
                  </Link>
                ))}
              </div>
            )}
          </Section>

          <Section title="Source status">
            <div className="card flex flex-wrap gap-x-4 gap-y-1 text-xs">
              {dashboard.data.source_health.map((row) => (
                <span key={row.source} title={row.last_error ?? ''}>
                  <span className={statusTone(row.status)}>●</span> {row.source}{' '}
                  <span className="text-muted">{row.status.toLowerCase()}</span>
                </span>
              ))}
            </div>
          </Section>

          <Section title="Notifications">
            <div className="grid grid-cols-3 gap-2">
              <Stat label="Unread" value={dashboard.data.notification_summary.unread} />
              <Stat label="Alerts 24h" value={dashboard.data.notification_summary.alerts_today} />
              <Stat
                label="Failed"
                value={dashboard.data.notification_summary.failed_deliveries}
                tone={dashboard.data.notification_summary.failed_deliveries > 0 ? 'text-bear' : ''}
              />
            </div>
            <p className="mt-2 text-[11px] text-muted">
              Push: {dashboard.data.notification_summary.push_status} · Digest:{' '}
              {dashboard.data.notification_summary.next_digest}
            </p>
          </Section>
        </>
      )}

      <Section
        title="Live feed"
        action={
          <div className="flex items-center gap-2">
            <label className="flex items-center gap-1 text-[11px] text-muted">
              <input
                type="checkbox"
                checked={includeFiltered}
                onChange={(e) => setIncludeFiltered(e.target.checked)}
              />
              filtered
            </label>
            <select
              className="rounded-lg border border-edge bg-panel px-2 py-1 text-xs"
              value={sort}
              onChange={(event) => setSort(event.target.value)}
              aria-label="Sort events"
            >
              {SORTS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </div>
        }
      >
        {events.loading && <Spinner />}
        {events.error && <ErrorBox message={events.error} onRetry={events.reload} />}
        {events.data?.items.length === 0 && (
          <Empty>
            Nothing yet. With the mock source enabled, run a poll from Settings → Operations.
          </Empty>
        )}
        {events.data?.items.map((event) => <EventCard key={event.id} event={event} />)}
      </Section>

      <Disclaimer />
    </div>
  );
}
