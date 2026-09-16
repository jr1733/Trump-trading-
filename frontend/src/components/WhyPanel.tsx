/* The "Why?" panel.
 *
 * Mandatory next to every signal. It shows each component value, the weight it
 * was given, the sample size, the historical statistics, and the uncertainties
 * -- so a score is never a number with no visible provenance.
 */

import { useState } from 'react';

import type { HorizonStat, Signal } from '../lib/api';
import { num, pct, signed, titleCase } from '../lib/format';
import { SampleFlag } from './ui';

function ComponentRow({
  name,
  value,
  weight,
  contribution,
}: {
  name: string;
  value: number;
  weight: number | undefined;
  contribution: number | undefined;
}) {
  return (
    <tr className="border-t border-edge/60">
      <td className="py-1.5 pr-2 text-gray-300">{titleCase(name)}</td>
      <td className="py-1.5 pr-2 text-right tabular-nums">{signed(value)}</td>
      <td className="py-1.5 pr-2 text-right tabular-nums text-muted">
        {weight === undefined ? '—' : `×${num(weight)}`}
      </td>
      <td className="py-1.5 text-right tabular-nums">{signed(contribution)}</td>
    </tr>
  );
}

function StatsTable({ stats }: { stats: Record<string, HorizonStat> }) {
  const rows = Object.values(stats).filter((s) => s.n > 0);
  if (rows.length === 0) {
    return <p className="text-xs text-muted">No comparable past events were found.</p>;
  }
  return (
    <div className="-mx-1 overflow-x-auto">
      <table className="w-full min-w-[420px] text-xs">
        <thead className="text-muted">
          <tr>
            <th className="py-1 pr-2 text-left font-medium">Horizon</th>
            <th className="py-1 pr-2 text-right font-medium">N</th>
            <th className="py-1 pr-2 text-right font-medium">Median</th>
            <th className="py-1 pr-2 text-right font-medium">Med. abn.</th>
            <th className="py-1 pr-2 text-right font-medium">Pos %</th>
            <th className="py-1 pr-2 text-right font-medium">SD</th>
            <th className="py-1 text-right font-medium">5–95%</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((stat) => (
            <tr key={stat.horizon} className="border-t border-edge/60">
              <td className="py-1.5 pr-2">
                {stat.horizon}
                {stat.flag !== 'ok' && (
                  <span className={stat.flag === 'limited' ? ' text-warn' : ' text-bear'}> *</span>
                )}
              </td>
              <td className="py-1.5 pr-2 text-right tabular-nums">{stat.n}</td>
              <td className="py-1.5 pr-2 text-right tabular-nums">{pct(stat.median)}</td>
              <td className="py-1.5 pr-2 text-right tabular-nums">{pct(stat.median_abnormal)}</td>
              <td className="py-1.5 pr-2 text-right tabular-nums">{num(stat.positive_pct, 0)}%</td>
              <td className="py-1.5 pr-2 text-right tabular-nums">{pct(stat.stdev)}</td>
              <td className="py-1.5 text-right tabular-nums">
                {pct(stat.p5, 1)} … {pct(stat.p95, 1)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="mt-2 text-[11px] text-muted">
        * <span className="text-warn">limited</span> (N &lt; 20) or{' '}
        <span className="text-bear">unreliable</span> (N &lt; 10) sample. Unreliable statistics are
        shown for transparency and given zero weight in the score.
      </p>
    </div>
  );
}

export default function WhyPanel({ signal, open: initial = false }: { signal: Signal; open?: boolean }) {
  const [open, setOpen] = useState(initial);

  const components = signal.components as Record<string, unknown>;
  const contributions = (components.contributions ?? {}) as Record<string, number>;
  const notes = (components.notes ?? []) as string[];
  const sentimentSource = String(components.sentiment_source ?? 'unknown');
  const sentimentModel = components.sentiment_model ? String(components.sentiment_model) : null;
  const similarityMeasure = String(components.similarity_measure ?? '');
  const maxSimilarity = components.max_similarity_30d as number | undefined;
  const stats = signal.historical_stats?.stats ?? {};

  const componentNames = ['sentiment', 'historical', 'consistency', 'novelty'].filter(
    (name) => typeof components[name] === 'number',
  );

  return (
    <div className="mt-3 rounded-lg border border-edge bg-bg/60">
      <button
        className="flex w-full items-center justify-between px-3 py-2 text-left text-sm font-medium"
        onClick={() => setOpen(!open)}
        aria-expanded={open}
      >
        <span>Why?</span>
        <span className="text-muted">{open ? '▾' : '▸'}</span>
      </button>

      {open && (
        <div className="space-y-4 border-t border-edge px-3 py-3">
          <div>
            <h4 className="label">Score components</h4>
            <table className="w-full text-xs">
              <thead className="text-muted">
                <tr>
                  <th className="py-1 pr-2 text-left font-medium">Component</th>
                  <th className="py-1 pr-2 text-right font-medium">Value</th>
                  <th className="py-1 pr-2 text-right font-medium">Weight</th>
                  <th className="py-1 text-right font-medium">Contribution</th>
                </tr>
              </thead>
              <tbody>
                {componentNames.map((name) => (
                  <ComponentRow
                    key={name}
                    name={name}
                    value={components[name] as number}
                    weight={signal.weights[name]}
                    contribution={contributions[name]}
                  />
                ))}
                <tr className="border-t border-edge">
                  <td className="py-1.5 pr-2 text-muted">× model confidence</td>
                  <td className="py-1.5 pr-2 text-right tabular-nums">{num(signal.confidence)}</td>
                  <td />
                  <td className="py-1.5 text-right font-semibold tabular-nums">
                    {signed(signal.score)}
                  </td>
                </tr>
              </tbody>
            </table>
            <div className="mt-2 flex flex-wrap items-center gap-2">
              <SampleFlag flag={signal.sample_flag} n={signal.sample_size} />
              <span className="chip bg-edge text-muted">
                sentiment: {sentimentSource}
                {sentimentModel ? ` (${sentimentModel})` : ''}
              </span>
              <span className="chip bg-edge text-muted">horizon: {signal.horizon}</span>
              {typeof maxSimilarity === 'number' && (
                <span className="chip bg-edge text-muted">
                  max similarity 30d: {num(maxSimilarity)}
                </span>
              )}
            </div>
            {similarityMeasure && (
              <p className="mt-2 text-[11px] text-muted">Similarity measure: {similarityMeasure}</p>
            )}
            {notes.map((note) => (
              <p key={note} className="mt-2 text-[11px] text-muted">
                {note}
              </p>
            ))}
          </div>

          <div>
            <h4 className="label">
              Historical response · {signal.historical_stats?.match_basis ?? 'event type + ticker'}
            </h4>
            <StatsTable stats={stats} />
          </div>

          <div>
            <h4 className="label text-warn">Important uncertainties</h4>
            <ul className="list-disc space-y-1 pl-4 text-xs leading-relaxed text-gray-300">
              {signal.uncertainties.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          </div>
        </div>
      )}
    </div>
  );
}
