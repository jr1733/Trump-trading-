import { useEffect, useState } from 'react';
import { useParams } from 'react-router-dom';

import WhyPanel from '../components/WhyPanel';
import { Disclaimer, Empty, ErrorBox, Section, Spinner, Stat } from '../components/ui';
import { api, type HorizonStat, type Signal } from '../lib/api';
import { labelColour, localTime, num, pct, signed, titleCase } from '../lib/format';
import { useApi } from '../lib/useApi';

interface EventHistoryRow {
  event_id: string;
  source_timestamp: string;
  title: string;
  source: string;
  event_type: string;
  is_historical: boolean;
  sentiment: number | null;
  returns: Record<string, { raw_return: number; abnormal_return: number | null; basis: string }>;
}

interface TickerDetailBody {
  symbol: string;
  name: string;
  sector: string | null;
  sector_etf: string | null;
  available_horizons: string[];
  provider: string;
  supports_intraday: boolean;
  signal: Signal | null;
  event_history: EventHistoryRow[];
  historical_stats: { stats?: Record<string, HorizonStat>; match_basis?: string };
}

interface ChartBody {
  symbol: string;
  resolution: string;
  anchor_ts: string;
  basis: string;
  series: Array<{ ts: string; close: number }>;
}

/** Inline sparkline. A dependency-free SVG beats a charting library here: the
 *  whole point is a small bundle on a phone. */
function Sparkline({ series, anchorIndex }: { series: number[]; anchorIndex: number }) {
  if (series.length < 2) return <p className="text-xs text-muted">Not enough price data to plot.</p>;
  const width = 320;
  const height = 90;
  const min = Math.min(...series);
  const max = Math.max(...series);
  const span = max - min || 1;
  const points = series
    .map((value, index) => {
      const x = (index / (series.length - 1)) * width;
      const y = height - ((value - min) / span) * height;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(' ');
  const anchorX = (anchorIndex / (series.length - 1)) * width;
  const rising = series[series.length - 1] >= series[0];

  return (
    <svg viewBox={`0 0 ${width} ${height}`} className="h-24 w-full" role="img" aria-label="price series">
      <polyline
        points={points}
        fill="none"
        stroke={rising ? '#3fb950' : '#f85149'}
        strokeWidth="2"
        vectorEffect="non-scaling-stroke"
      />
      {anchorIndex >= 0 && (
        <line x1={anchorX} y1="0" x2={anchorX} y2={height} stroke="#388bfd" strokeDasharray="3 3" />
      )}
    </svg>
  );
}

export default function TickerDetail() {
  const { symbol = '' } = useParams();
  const upper = symbol.toUpperCase();
  const detail = useApi<TickerDetailBody>(`/tickers/${upper}`);
  const chart = useApi<ChartBody>(`/tickers/${upper}/chart`);
  const watchlist = useApi<{ items: Array<{ ticker: string }> }>('/watchlist');
  const [watched, setWatched] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (watchlist.data) setWatched(watchlist.data.items.some((row) => row.ticker === upper));
  }, [watchlist.data, upper]);

  async function toggleWatch() {
    setBusy(true);
    try {
      if (watched) {
        await api.del(`/watchlist/${upper}`);
        setWatched(false);
      } else {
        await api.post('/watchlist', { ticker: upper });
        setWatched(true);
      }
    } catch {
      /* leave the toggle as it was; the next load reconciles */
    } finally {
      setBusy(false);
    }
  }

  if (detail.loading) return <Spinner />;
  if (detail.error) return <ErrorBox message={detail.error} onRetry={detail.reload} />;
  if (!detail.data) return null;

  const { data } = detail;
  const stats = Object.values(data.historical_stats?.stats ?? {}).filter((s) => s.n > 0);
  const anchorIndex = chart.data
    ? Math.max(
        0,
        chart.data.series.findIndex((point) => point.ts >= chart.data!.anchor_ts),
      )
    : -1;

  return (
    <div>
      <header className="mb-4 flex items-start justify-between gap-3">
        <div>
          <h1 className="text-xl font-bold">{data.symbol}</h1>
          <p className="text-sm text-muted">{data.name}</p>
          <p className="mt-1 text-[11px] text-muted">
            {data.sector ?? '—'}
            {data.sector_etf ? ` · benchmarked against ${data.sector_etf}` : ''}
          </p>
        </div>
        <button className="btn shrink-0" onClick={toggleWatch} disabled={busy || watched === null}>
          {watched ? '★ Watching' : '☆ Watch'}
        </button>
      </header>

      {data.signal ? (
        <Section title="Current signal">
          <div className="card">
            <div className="flex items-baseline justify-between">
              <span className={`text-lg font-bold ${labelColour(data.signal.label)}`}>
                {data.signal.label}
              </span>
              <span className="text-lg font-bold tabular-nums">{signed(data.signal.score)}</span>
            </div>
            <div className="mt-1 text-xs text-muted">
              horizon {data.signal.horizon} · confidence {num(data.signal.confidence)} · computed{' '}
              {localTime(data.signal.created_at)}
            </div>
            <WhyPanel signal={data.signal} open />
          </div>
        </Section>
      ) : (
        <Section title="Current signal">
          <Empty>No signal has been computed for {data.symbol} yet.</Empty>
        </Section>
      )}

      <Section title={`Price · ${chart.data?.resolution ?? '…'}`}>
        <div className="card">
          {chart.loading && <Spinner label="Loading prices" />}
          {chart.error && <p className="text-xs text-bear">{chart.error}</p>}
          {chart.data && (
            <>
              <Sparkline series={chart.data.series.map((p) => p.close)} anchorIndex={anchorIndex} />
              <p className="mt-1 text-[11px] text-muted">
                Provider: {data.provider}
                {data.supports_intraday ? '' : ' (daily bars only — intraday horizons unavailable)'}
                . Dashed line marks the event anchor ({chart.data.basis.replace('_', ' ')}).
              </p>
            </>
          )}
        </div>
      </Section>

      <Section title="Historical response">
        {stats.length === 0 ? (
          <Empty>No comparable past events for {data.symbol} yet.</Empty>
        ) : (
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
            {stats.map((stat) => (
              <Stat
                key={stat.horizon}
                label={`${stat.horizon} · N=${stat.n}`}
                value={
                  <span className={stat.flag === 'ok' ? '' : stat.flag === 'limited' ? 'text-warn' : 'text-bear'}>
                    {pct(stat.median)}
                  </span>
                }
              />
            ))}
          </div>
        )}
      </Section>

      <Section title="Event history">
        {data.event_history.length === 0 ? (
          <Empty>No events reference {data.symbol}.</Empty>
        ) : (
          <div className="-mx-1 overflow-x-auto">
            <table className="w-full min-w-[480px] text-xs">
              <thead className="text-muted">
                <tr>
                  <th className="py-1 pr-2 text-left font-medium">Date</th>
                  <th className="py-1 pr-2 text-left font-medium">Event</th>
                  <th className="py-1 pr-2 text-right font-medium">Sent.</th>
                  <th className="py-1 pr-2 text-right font-medium">1d</th>
                  <th className="py-1 text-right font-medium">5d</th>
                </tr>
              </thead>
              <tbody>
                {data.event_history.map((row) => {
                  const oneDay = row.returns['1d']?.raw_return ?? null;
                  const fiveDay = row.returns['5d']?.raw_return ?? null;
                  return (
                    <tr key={row.event_id} className="border-t border-edge/60 align-top">
                      <td className="py-1.5 pr-2 whitespace-nowrap text-muted">
                        {localTime(row.source_timestamp)}
                      </td>
                      <td className="py-1.5 pr-2">
                        <span className="line-clamp-2">{row.title}</span>
                        <span className="text-[10px] text-muted">
                          {titleCase(row.event_type)}
                          {row.is_historical ? ' · archive' : ''}
                        </span>
                      </td>
                      <td className="py-1.5 pr-2 text-right tabular-nums">{signed(row.sentiment)}</td>
                      <td
                        className={`py-1.5 pr-2 text-right tabular-nums ${
                          (oneDay ?? 0) >= 0 ? 'text-bull' : 'text-bear'
                        }`}
                      >
                        {pct(oneDay)}
                      </td>
                      <td
                        className={`py-1.5 text-right tabular-nums ${
                          (fiveDay ?? 0) >= 0 ? 'text-bull' : 'text-bear'
                        }`}
                      >
                        {pct(fiveDay)}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Section>

      <Disclaimer />
    </div>
  );
}
