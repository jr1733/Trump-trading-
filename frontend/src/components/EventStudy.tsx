/* Market-model event study for a ticker.
 *
 * Shows CAAR per event window with its t-statistic, and says plainly when a
 * result is not significant -- which, on small samples of political events, is
 * the usual and expected outcome.
 */

import { useState } from 'react';

import { Empty, SampleFlag, Spinner } from './ui';
import { num, pct } from '../lib/format';
import { useApi } from '../lib/useApi';

interface WindowStat {
  window: string;
  n: number;
  caar: number;
  median_car: number;
  sd: number;
  positive_pct: number;
  t_stat: number | null;
  significant_5pct: boolean;
  flag: 'ok' | 'limited' | 'unreliable';
}

interface EventStudyBody {
  ticker: string;
  event_type: string | null;
  n_events: number;
  sample_flag: 'ok' | 'limited' | 'unreliable';
  benchmark: string;
  windows: Record<string, WindowStat>;
  events: Array<{ event_id: string; model: { beta: number; r_squared: number } }>;
  skipped: Array<{ event_id: string; reason: string }>;
  notes: string[];
}

export default function EventStudy({ symbol }: { symbol: string }) {
  const [open, setOpen] = useState(false);
  const { data, error, loading } = useApi<EventStudyBody>(
    open ? `/tickers/${symbol}/event-study` : null,
  );

  if (!open) {
    return (
      <button className="btn w-full" onClick={() => setOpen(true)}>
        Run event study
      </button>
    );
  }

  if (loading) return <Spinner label="Estimating market models" />;
  if (error) return <p className="text-xs text-bear">{error}</p>;
  if (!data) return null;

  const rows = Object.values(data.windows);
  const averageBeta =
    data.events.length > 0
      ? data.events.reduce((sum, e) => sum + e.model.beta, 0) / data.events.length
      : null;

  return (
    <div className="card">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <SampleFlag flag={data.sample_flag} n={data.n_events} />
        <span className="chip bg-edge text-muted">vs {data.benchmark}</span>
        {averageBeta !== null && (
          <span className="chip bg-edge text-muted">mean β {num(averageBeta)}</span>
        )}
      </div>

      {rows.length === 0 ? (
        <Empty>Not enough price history to estimate a market model for {data.ticker}.</Empty>
      ) : (
        <div className="-mx-1 overflow-x-auto">
          <table className="w-full min-w-[420px] text-xs">
            <thead className="text-muted">
              <tr>
                <th className="py-1 pr-2 text-left font-medium">Window</th>
                <th className="py-1 pr-2 text-right font-medium">N</th>
                <th className="py-1 pr-2 text-right font-medium">CAAR</th>
                <th className="py-1 pr-2 text-right font-medium">Median</th>
                <th className="py-1 pr-2 text-right font-medium">Pos %</th>
                <th className="py-1 text-right font-medium">t</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.window} className="border-t border-edge/60">
                  <td className="py-1.5 pr-2 tabular-nums">{row.window}</td>
                  <td className="py-1.5 pr-2 text-right tabular-nums">{row.n}</td>
                  <td
                    className={`py-1.5 pr-2 text-right tabular-nums ${
                      row.caar >= 0 ? 'text-bull' : 'text-bear'
                    }`}
                  >
                    {pct(row.caar)}
                  </td>
                  <td className="py-1.5 pr-2 text-right tabular-nums">{pct(row.median_car)}</td>
                  <td className="py-1.5 pr-2 text-right tabular-nums">{num(row.positive_pct, 0)}%</td>
                  <td className="py-1.5 text-right tabular-nums">
                    {row.t_stat === null ? (
                      <span className="text-muted">—</span>
                    ) : (
                      <span className={row.significant_5pct ? 'text-bull' : 'text-muted'}>
                        {num(row.t_stat)}
                      </span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          <p className="mt-2 text-[11px] text-muted">
            A t-statistic above 1.96 in absolute value would be significant at 5%. It is shown as
            “—” when the sample is too small for the number to mean anything. On samples this size,
            “not significant” is the expected result and should be read as “no detectable effect”,
            not as “no effect”.
          </p>
        </div>
      )}

      {data.skipped.length > 0 && (
        <p className="mt-2 text-[11px] text-warn">
          {data.skipped.length} event(s) skipped for want of a usable estimation window.
        </p>
      )}
      {data.notes.map((note) => (
        <p key={note} className="mt-2 text-[11px] text-muted">
          {note}
        </p>
      ))}
    </div>
  );
}
