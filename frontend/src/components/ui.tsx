import { ReactNode } from 'react';

import { flagLabel } from '../lib/format';
import { analysisIsMock, marketIsMock, useConfig } from '../lib/useConfig';

/* Mock-data labelling.
 *
 * Out of the box every price is synthetic and every analysis is canned. Those
 * numbers look exactly like real ones -- same decimals, same colours, same
 * confident little percentages -- so the only thing stopping someone reading a
 * demo as a finding is saying so, in front of the numbers, on every page.
 */

/** Page-level banner. Rendered once in the app shell, above the routed page. */
export function MockDataBanner() {
  const config = useConfig();
  const market = marketIsMock(config);
  const analysis = analysisIsMock(config);
  if (!config || (!market && !analysis)) return null;

  const what = market && analysis ? 'Prices and analyses are' : market ? 'Prices are' : 'Analyses are';
  return (
    <div className="mx-4 mt-3 rounded-lg border border-warn/60 bg-warn/10 px-3 py-2">
      <p className="text-xs font-bold text-warn">⚠ DEMO DATA — not real market data</p>
      <p className="mt-0.5 text-[11px] leading-relaxed text-gray-300">
        {what} synthetic.{' '}
        {market && 'Every price, return and statistic on every page is generated, not observed. '}
        {analysis && 'Analyses come from the offline canned scorer, not a language model. '}
        Nothing here describes anything that happened in a real market.
      </p>
    </div>
  );
}

/** Inline badge for a specific number or panel. */
export function MockBadge({ kind = 'market' }: { kind?: 'market' | 'analysis' }) {
  const config = useConfig();
  const isMock = kind === 'market' ? marketIsMock(config) : analysisIsMock(config);
  if (!isMock) return null;
  return (
    <span
      className="chip bg-warn/15 text-warn"
      title={
        kind === 'market'
          ? 'Synthetic prices from the mock provider, not a real market feed.'
          : 'Canned offline analysis, not a language model reading.'
      }
    >
      {kind === 'market' ? 'MOCK PRICES' : 'MOCK ANALYSIS'}
    </span>
  );
}

export function Spinner({ label = 'Loading' }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 p-6 text-sm text-muted">
      <span className="h-3 w-3 animate-pulse rounded-full bg-accent" />
      {label}…
    </div>
  );
}

export function ErrorBox({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="card border-bear/40">
      <p className="text-sm text-bear">{message}</p>
      {onRetry && (
        <button className="btn mt-3" onClick={onRetry}>
          Try again
        </button>
      )}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="card text-sm text-muted">{children}</div>;
}

export function SampleFlag({ flag, n }: { flag: string; n: number }) {
  const { text, className } = flagLabel(flag);
  return (
    <span className={`chip ${className}`}>
      N={n} · {text}
    </span>
  );
}

export function Section({ title, action, children }: { title: string; action?: ReactNode; children: ReactNode }) {
  return (
    <section className="mb-5">
      <div className="mb-2 flex items-baseline justify-between">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-muted">{title}</h2>
        {action}
      </div>
      {children}
    </section>
  );
}

export function Stat({ label, value, tone }: { label: string; value: ReactNode; tone?: string }) {
  return (
    <div className="rounded-lg border border-edge bg-bg px-3 py-2">
      <div className="text-[11px] uppercase tracking-wide text-muted">{label}</div>
      <div className={`text-sm font-semibold ${tone ?? ''}`}>{value}</div>
    </div>
  );
}

/** The standing disclaimer. Rendered on every page that shows a signal. */
export function Disclaimer() {
  return (
    <p className="px-1 py-4 text-[11px] leading-relaxed text-muted">
      Research and information tool. These are statistical associations between past announcements
      and past price moves, computed from small samples. Association is not causation and none of
      this is a forecast or a recommendation.
    </p>
  );
}
