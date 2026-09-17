/* Data quality and cost.
 *
 * One page for "what is wrong or missing" and "what is this costing". Both
 * exist so problems are visible without reading logs -- a silently degraded
 * source or a stalled analysis queue is exactly the failure mode that makes a
 * monitoring tool quietly useless.
 */

import { Link } from 'react-router-dom';

import { ErrorBox, Section, Spinner, Stat } from '../components/ui';
import { localTime, num, titleCase } from '../lib/format';
import { useApi } from '../lib/useApi';

interface DataQualityBody {
  unprocessed_raw_events: number;
  events_without_current_embedding: number;
  duplicate_content_hashes: number;
  future_timestamps: number;
  pending_or_failed_analyses: number;
  ticker_corrections: number;
  low_confidence_ticker_matches: number;
  expired_push_subscriptions: number;
  stale_sources: string[];
  market_data: Array<{ symbol: string; latest_bar: string | null; stale: boolean }>;
  process_errors: Array<{ id: string; source: string; error: string }>;
  malformed_analyses: Array<{
    id: string;
    event_id: string | null;
    model: string;
    attempts: number;
    error: string | null;
  }>;
  failed_deliveries: Array<{
    channel: string;
    status: string;
    response: string | null;
    created_at: string;
  }>;
  failed_jobs: Array<{ name: string; started_at: string; error: string | null }>;
  llm_usage_7d: {
    calls: number;
    input_tokens: number;
    output_tokens: number;
    estimated_cost_usd: number;
    daily_call_budget: number;
  };
  by_source_7d: Array<{
    source: string;
    events: number;
    relevant: number;
    model_calls: number;
    estimated_cost_usd: number;
  }>;
}

interface ShadowBody {
  enabled: boolean;
  model: string | null;
  primary_model?: string;
  sample_rate: number;
  compared: number;
  shadow_rows: number;
  mean_abs_sentiment_diff: number | null;
  mean_abs_confidence_diff: number | null;
  direction_disagreement_pct: number | null;
  event_type_disagreement_pct: number | null;
  estimated_cost_usd: number;
  examples: Array<{
    event_id: string | null;
    primary_sentiment: number;
    shadow_sentiment: number;
    primary_event_type: string | null;
    shadow_event_type: string | null;
  }>;
}

interface JobsBody {
  items: Array<{
    id: string;
    job_name: string;
    status: string;
    started_at: string;
    finished_at: string | null;
    items_processed: number;
    error: string | null;
  }>;
}

function Problem({ label, value, tone }: { label: string; value: number; tone?: boolean }) {
  return (
    <Stat
      label={label}
      value={value}
      tone={tone && value > 0 ? 'text-bear' : value > 0 ? 'text-warn' : 'text-bull'}
    />
  );
}

export default function DataQuality() {
  const quality = useApi<DataQualityBody>('/admin/data-quality');
  const jobs = useApi<JobsBody>('/admin/jobs?limit=15');
  const shadow = useApi<ShadowBody>('/admin/shadow-comparison');

  if (quality.loading) return <Spinner label="Checking data quality" />;
  if (quality.error) return <ErrorBox message={quality.error} onRetry={quality.reload} />;
  if (!quality.data) return null;

  const q = quality.data;
  const usage = q.llm_usage_7d;
  // Seven days of spend, extrapolated. Stated as an extrapolation, not a bill.
  const monthlyEstimate = (usage.estimated_cost_usd / 7) * 30;

  return (
    <div>
      <Link to="/settings" className="mb-3 inline-block text-sm text-accent">
        ← Back to settings
      </Link>

      <Section title="Pipeline">
        <div className="grid grid-cols-2 gap-2">
          <Problem label="Unprocessed raw" value={q.unprocessed_raw_events} />
          <Problem label="Pending / failed analyses" value={q.pending_or_failed_analyses} />
          <Problem label="Missing embeddings" value={q.events_without_current_embedding} />
          <Problem label="Duplicate content hashes" value={q.duplicate_content_hashes} />
          <Problem label="Future timestamps" value={q.future_timestamps} tone />
          <Problem label="Expired push subs" value={q.expired_push_subscriptions} />
        </div>
        <p className="mt-2 text-[11px] text-muted">
          Duplicate content hashes are expected and healthy — they are the same story arriving
          from two sources, collapsed into one event. Future timestamps are not.
        </p>
      </Section>

      <Section title="Ticker matching">
        <div className="grid grid-cols-2 gap-2">
          <Stat label="User corrections stored" value={q.ticker_corrections} />
          <Stat label="LOW-confidence matches" value={q.low_confidence_ticker_matches} />
        </div>
        <p className="mt-2 text-[11px] text-muted">
          LOW-confidence matches are shown in the UI but excluded from signals and statistics.
          Corrections you make are stored and reused.
        </p>
      </Section>

      <Section title="Market data">
        <div className="card text-xs">
          {q.market_data.map((row) => (
            <div key={row.symbol} className="flex items-center justify-between py-1">
              <span className="font-medium text-gray-200">{row.symbol}</span>
              <span className={row.stale ? 'text-warn' : 'text-muted'}>
                {row.latest_bar ? localTime(row.latest_bar) : 'no data'}
                {row.stale ? ' · stale' : ''}
              </span>
            </div>
          ))}
        </div>
      </Section>

      {q.stale_sources.length > 0 && (
        <Section title="Stale sources">
          <div className="card text-xs text-warn">
            {q.stale_sources.join(', ')} — no successful poll in over six hours.
          </div>
        </Section>
      )}

      <Section title="Model cost (last 7 days)">
        <div className="grid grid-cols-2 gap-2">
          <Stat label="Calls" value={usage.calls} />
          <Stat label="Estimated cost" value={`$${num(usage.estimated_cost_usd, 4)}`} />
          <Stat label="Input tokens" value={usage.input_tokens.toLocaleString()} />
          <Stat label="Output tokens" value={usage.output_tokens.toLocaleString()} />
        </div>
        <p className="mt-2 text-[11px] text-muted">
          At this rate, roughly <strong>${num(monthlyEstimate, 2)}/month</strong> — an
          extrapolation from seven days, not a bill. Daily call budget:{' '}
          {usage.daily_call_budget}. Costs are estimated from published per-token rates at the
          time of the call; check your provider invoice for the real figure.
        </p>
      </Section>

      {q.by_source_7d.length > 0 && (
        <Section title="Volume and spend by source (7 days)">
          <div className="card">
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead className="text-muted">
                  <tr>
                    <th className="py-1 pr-2 text-left font-medium">Source</th>
                    <th className="py-1 pr-1 text-right font-medium">Events</th>
                    <th className="py-1 pr-1 text-right font-medium">Kept</th>
                    <th className="py-1 pr-1 text-right font-medium">Calls</th>
                    <th className="py-1 text-right font-medium">Cost</th>
                  </tr>
                </thead>
                <tbody>
                  {q.by_source_7d.map((row) => (
                    <tr key={row.source} className="border-t border-edge/60">
                      <td className="py-1.5 pr-2">{row.source}</td>
                      <td className="py-1.5 pr-1 text-right tabular-nums">{row.events}</td>
                      <td className="py-1.5 pr-1 text-right tabular-nums text-muted">
                        {row.relevant}
                      </td>
                      <td className="py-1.5 pr-1 text-right tabular-nums">{row.model_calls}</td>
                      <td className="py-1.5 text-right tabular-nums">
                        ${num(row.estimated_cost_usd, 4)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="mt-2 text-[11px] leading-relaxed text-muted">
              <strong>Kept</strong> is how many passed the relevance gate. Congress is the
              source to watch here: it moves hundreds of bills a week, so its adapter applies
              a keyword filter <em>before</em> anything is stored. Many events and few calls
              means that filter is doing its job; many calls means it is not, and the
              threshold wants raising.
            </p>
          </div>
        </Section>
      )}

      {shadow.data?.enabled && (
        <Section title="Model comparison">
          <div className="card space-y-2">
            <div className="flex flex-wrap items-center gap-2 text-xs">
              <span className="chip bg-edge text-muted">
                primary {shadow.data.primary_model ?? '—'}
              </span>
              <span className="chip bg-edge text-muted">shadow {shadow.data.model}</span>
              <span className="chip bg-edge text-muted">
                {num(shadow.data.sample_rate * 100, 0)}% sampled
              </span>
            </div>

            {shadow.data.compared === 0 ? (
              <p className="text-xs text-muted">
                {shadow.data.shadow_rows} shadow analysis(es) stored, none yet paired with a
                completed primary analysis. The comparison fills in as events are processed.
              </p>
            ) : (
              <>
                <div className="grid grid-cols-2 gap-2">
                  <Stat label="Events compared" value={shadow.data.compared} />
                  <Stat
                    label="Direction disagreement"
                    value={`${num(shadow.data.direction_disagreement_pct, 1)}%`}
                    tone={
                      (shadow.data.direction_disagreement_pct ?? 0) > 20
                        ? 'text-warn'
                        : 'text-bull'
                    }
                  />
                  <Stat
                    label="Mean |sentiment diff|"
                    value={num(shadow.data.mean_abs_sentiment_diff, 3)}
                  />
                  <Stat
                    label="Shadow cost so far"
                    value={`$${num(shadow.data.estimated_cost_usd, 4)}`}
                  />
                </div>
                <p className="text-[11px] leading-relaxed text-muted">
                  <strong>Direction disagreement</strong> is the number that matters: two
                  models differing by 0.1 on sentiment changes nothing, while one saying
                  bullish and the other bearish changes the signal. A low figure here is the
                  argument for staying on the cheaper model.
                </p>
                {shadow.data.examples.length > 0 && (
                  <div className="space-y-1 text-xs">
                    <div className="text-muted">Biggest disagreements:</div>
                    {shadow.data.examples.map((row, index) => (
                      <div key={`${row.event_id}-${index}`} className="flex justify-between gap-2">
                        <span>
                          {row.event_id ? (
                            <Link to={`/event/${row.event_id}`} className="text-accent">
                              view event
                            </Link>
                          ) : (
                            <span className="text-muted">(no event)</span>
                          )}
                        </span>
                        <span className="tabular-nums text-muted">
                          {num(row.primary_sentiment, 2)} vs {num(row.shadow_sentiment, 2)}
                        </span>
                      </div>
                    ))}
                  </div>
                )}
              </>
            )}
            <p className="text-[11px] text-muted">
              Shadow results are stored in their own table and are never read by anything that
              builds a signal, an alert or a backtest.
            </p>
          </div>
        </Section>
      )}

      {q.malformed_analyses.length > 0 && (
        <Section title="Malformed model output">
          <div className="card space-y-2 text-xs">
            {q.malformed_analyses.map((row) => (
              <div key={row.id}>
                <div className="text-gray-300">
                  {row.model} · {row.attempts} attempt(s)
                  {row.event_id && (
                    <Link to={`/event/${row.event_id}`} className="ml-2 text-accent">
                      view event
                    </Link>
                  )}
                </div>
                <div className="text-bear">{(row.error || '').slice(0, 200)}</div>
              </div>
            ))}
          </div>
        </Section>
      )}

      {q.process_errors.length > 0 && (
        <Section title="Processing errors">
          <div className="card space-y-1 text-xs">
            {q.process_errors.map((row) => (
              <div key={row.id}>
                <span className="text-muted">{row.source}: </span>
                <span className="text-bear">{row.error.slice(0, 180)}</span>
              </div>
            ))}
          </div>
        </Section>
      )}

      {q.failed_deliveries.length > 0 && (
        <Section title="Failed deliveries">
          <div className="card space-y-1 text-xs">
            {q.failed_deliveries.map((row, index) => (
              <div key={`${row.created_at}-${index}`}>
                <span className="text-muted">
                  {localTime(row.created_at)} {row.channel}{' '}
                </span>
                <span className="text-bear">
                  {row.status}: {(row.response || '').slice(0, 140)}
                </span>
              </div>
            ))}
          </div>
        </Section>
      )}

      <Section title="Recent worker jobs">
        {jobs.loading && <Spinner label="Loading jobs" />}
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead className="text-muted">
              <tr>
                <th className="py-1 pr-2 text-left font-medium">Job</th>
                <th className="py-1 pr-2 text-left font-medium">Started</th>
                <th className="py-1 pr-1 text-right font-medium">N</th>
                <th className="py-1 text-right font-medium">Status</th>
              </tr>
            </thead>
            <tbody>
              {jobs.data?.items.map((job) => (
                <tr key={job.id} className="border-t border-edge/60">
                  <td className="py-1.5 pr-2">{titleCase(job.job_name)}</td>
                  <td className="py-1.5 pr-2 text-muted">{localTime(job.started_at)}</td>
                  <td className="py-1.5 pr-1 text-right tabular-nums">{job.items_processed}</td>
                  <td
                    className={`py-1.5 text-right ${
                      job.status === 'SUCCESS'
                        ? 'text-bull'
                        : job.status === 'FAILED'
                          ? 'text-bear'
                          : 'text-muted'
                    }`}
                  >
                    {job.status}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {q.failed_jobs.length > 0 && (
          <p className="mt-2 text-[11px] text-bear">
            {q.failed_jobs.length} job(s) failed in the last 7 days. Most recent:{' '}
            {(q.failed_jobs[0].error || '').slice(0, 160)}
          </p>
        )}
      </Section>
    </div>
  );
}
