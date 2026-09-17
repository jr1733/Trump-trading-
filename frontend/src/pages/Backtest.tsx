import { FormEvent, useState } from 'react';

import { Disclaimer, Empty, ErrorBox, SampleFlag, Section, Spinner } from '../components/ui';
import { api } from '../lib/api';
import { num, pct } from '../lib/format';

interface MetricBlock {
  n: number;
  flag: 'ok' | 'limited' | 'unreliable';
  mean: number | null;
  median: number | null;
  stdev: number | null;
  win_rate: number | null;
  max_drawdown: number | null;
  sharpe: number | null;
  best: number | null;
  worst: number | null;
  long_count: number;
  short_count: number;
  overlap_fraction: number;
}

interface SplitBlock extends MetricBlock {
  start: string;
  end: string;
}

interface BacktestBody {
  run_id: string;
  params: Record<string, unknown>;
  overall: MetricBlock;
  by_split: Record<string, SplitBlock>;
  skipped_count: number;
  llm_contaminated: boolean;
  notes: string[];
  warnings: string[];
}

const TODAY = new Date().toISOString().slice(0, 10);
const THREE_YEARS_AGO = new Date(Date.now() - 3 * 365 * 86400_000).toISOString().slice(0, 10);

function MetricRow({ label, block }: { label: string; block: MetricBlock }) {
  return (
    <tr className="border-t border-edge/60">
      <td className="py-1.5 pr-2">{label}</td>
      <td className="py-1.5 pr-2 text-right tabular-nums">{block.n}</td>
      <td
        className={`py-1.5 pr-2 text-right tabular-nums ${
          (block.mean ?? 0) >= 0 ? 'text-bull' : 'text-bear'
        }`}
      >
        {pct(block.mean)}
      </td>
      <td className="py-1.5 pr-2 text-right tabular-nums">{pct(block.median)}</td>
      <td className="py-1.5 pr-2 text-right tabular-nums">
        {block.win_rate === null ? '—' : `${num(block.win_rate, 0)}%`}
      </td>
      <td className="py-1.5 pr-2 text-right tabular-nums">{pct(block.stdev)}</td>
      <td className="py-1.5 pr-2 text-right tabular-nums text-bear">{pct(block.max_drawdown)}</td>
      <td className="py-1.5 text-right tabular-nums">
        {block.sharpe === null ? <span className="text-muted">—</span> : num(block.sharpe)}
      </td>
    </tr>
  );
}

export default function Backtest() {
  const [start, setStart] = useState(THREE_YEARS_AGO);
  const [end, setEnd] = useState(TODAY);
  const [ticker, setTicker] = useState('');
  const [eventType, setEventType] = useState('');
  const [minSignal, setMinSignal] = useState(0.2);
  const [holdingDays, setHoldingDays] = useState(5);
  const [sentimentMode, setSentimentMode] = useState<'rule_based' | 'llm'>('rule_based');

  const [result, setResult] = useState<BacktestBody | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function run(event: FormEvent) {
    event.preventDefault();
    setLoading(true);
    setError(null);
    try {
      const body = await api.post<BacktestBody>('/backtest?include_observations=false', {
        start,
        end,
        ticker: ticker.trim() || null,
        event_type: eventType.trim() || null,
        min_signal: minSignal,
        holding_days: holdingDays,
        sentiment_mode: sentimentMode,
      });
      setResult(body);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div>
      <Section title="Backtest">
        <form onSubmit={run} className="card space-y-3">
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="label" htmlFor="bt-start">
                Start
              </label>
              <input
                id="bt-start"
                className="input"
                type="date"
                value={start}
                onChange={(e) => setStart(e.target.value)}
              />
            </div>
            <div>
              <label className="label" htmlFor="bt-end">
                End
              </label>
              <input
                id="bt-end"
                className="input"
                type="date"
                value={end}
                onChange={(e) => setEnd(e.target.value)}
              />
            </div>
            <div>
              <label className="label" htmlFor="bt-ticker">
                Ticker (optional)
              </label>
              <input
                id="bt-ticker"
                className="input"
                value={ticker}
                onChange={(e) => setTicker(e.target.value)}
                placeholder="all"
                autoCapitalize="characters"
              />
            </div>
            <div>
              <label className="label" htmlFor="bt-type">
                Event type (optional)
              </label>
              <input
                id="bt-type"
                className="input"
                value={eventType}
                onChange={(e) => setEventType(e.target.value)}
                placeholder="all"
              />
            </div>
            <div>
              <label className="label" htmlFor="bt-min">
                Min |signal|
              </label>
              <input
                id="bt-min"
                className="input"
                type="number"
                step="0.05"
                min={0}
                max={1}
                value={minSignal}
                onChange={(e) => setMinSignal(Number(e.target.value))}
              />
            </div>
            <div>
              <label className="label" htmlFor="bt-hold">
                Holding period (sessions)
              </label>
              <input
                id="bt-hold"
                className="input"
                type="number"
                min={1}
                max={60}
                value={holdingDays}
                onChange={(e) => setHoldingDays(Number(e.target.value))}
              />
            </div>
          </div>

          <div>
            <label className="label" htmlFor="bt-mode">
              Sentiment source
            </label>
            <select
              id="bt-mode"
              className="input"
              value={sentimentMode}
              onChange={(e) => setSentimentMode(e.target.value as 'rule_based' | 'llm')}
            >
              <option value="rule_based">Rule-based lexicon (no LLM — clean)</option>
              <option value="llm">Model analysis (potentially contaminated)</option>
            </select>
            <p className="mt-1 text-[11px] text-muted">
              {sentimentMode === 'rule_based'
                ? 'No language model is involved, so there is no training-data contamination.'
                : 'The model may already know what followed these events. Results carry a contamination label.'}
            </p>
          </div>

          <button className="btn-primary w-full" disabled={loading}>
            {loading ? 'Running…' : 'Run backtest'}
          </button>
          <p className="text-[11px] text-muted">
            Every signal is recomputed point-in-time, so this takes a few seconds.
          </p>
        </form>
      </Section>

      {loading && <Spinner label="Recomputing signals point-in-time" />}
      {error && <ErrorBox message={error} />}

      {result && (
        <>
          {result.llm_contaminated && (
            <div className="card mb-4 border-warn bg-warn/5">
              <h3 className="text-sm font-bold text-warn">⚠ Potentially contaminated</h3>
              <p className="mt-1 text-xs text-gray-300">
                This run scored events with a language model whose training data may already
                contain knowledge of what followed them. Treat it as an upper bound, not a
                measurement. The rule-based mode is the clean comparison.
              </p>
            </div>
          )}

          {result.warnings.map((warning) => (
            <div key={warning} className="card mb-2 border-warn/50 text-xs text-warn">
              {warning}
            </div>
          ))}

          <Section title="Results">
            {result.overall.n === 0 ? (
              <Empty>No events cleared the threshold in this window.</Empty>
            ) : (
              <div className="card">
                <div className="mb-2 flex flex-wrap items-center gap-2">
                  <SampleFlag flag={result.overall.flag} n={result.overall.n} />
                  <span className="chip bg-edge text-muted">
                    {result.overall.long_count} long-direction / {result.overall.short_count} short
                  </span>
                  <span className="chip bg-edge text-muted">
                    {num(result.overall.overlap_fraction * 100, 0)}% overlapping windows
                  </span>
                </div>

                {/* Eight columns do not fit a phone. Rather than hide three of
                    them, the table scrolls and says so -- a silently truncated
                    table is how a reader misses the drawdown. */}
                <p className="mb-1 text-[11px] text-muted">Swipe the table sideways →</p>
                <div className="-mx-1 overflow-x-auto">
                  <table className="w-full min-w-[540px] text-xs">
                    <thead className="text-muted">
                      <tr>
                        <th className="py-1 pr-2 text-left font-medium">Period</th>
                        <th className="py-1 pr-2 text-right font-medium">N</th>
                        <th className="py-1 pr-2 text-right font-medium">Mean</th>
                        <th className="py-1 pr-2 text-right font-medium">Median</th>
                        <th className="py-1 pr-2 text-right font-medium">Win</th>
                        <th className="py-1 pr-2 text-right font-medium">SD</th>
                        <th className="py-1 pr-2 text-right font-medium">Max DD</th>
                        <th className="py-1 text-right font-medium">Sharpe</th>
                      </tr>
                    </thead>
                    <tbody>
                      <MetricRow label="All" block={result.overall} />
                      {(['train', 'validation', 'test'] as const).map((name) =>
                        result.by_split[name] ? (
                          <MetricRow key={name} label={name} block={result.by_split[name]} />
                        ) : null,
                      )}
                    </tbody>
                  </table>
                </div>

                <p className="mt-2 text-[11px] text-muted">
                  Splits are chronological, never random: shuffling time-series observations into
                  random folds lets the future inform the past. Tune on train, confirm on
                  validation, and look at test once.
                </p>
                <p className="mt-1 text-[11px] text-muted">
                  Best {pct(result.overall.best)} · worst {pct(result.overall.worst)} ·{' '}
                  {result.skipped_count} observation(s) skipped for missing data.
                </p>
              </div>
            )}
          </Section>

          <Section title="What this does and does not measure">
            <div className="card space-y-2 text-[11px] leading-relaxed text-muted">
              {result.notes.map((note) => (
                <p key={note}>{note}</p>
              ))}
            </div>
          </Section>
        </>
      )}

      <Disclaimer />
    </div>
  );
}
