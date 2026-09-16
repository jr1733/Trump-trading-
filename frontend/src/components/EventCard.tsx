import { Link } from 'react-router-dom';

import type { EventItem, Signal } from '../lib/api';
import { confidenceChip, labelColour, localTime, num, pct, relativeTime, signed, titleCase } from '../lib/format';
import WhyPanel from './WhyPanel';
import { SampleFlag } from './ui';

function SignalRow({ signal }: { signal: Signal }) {
  const stat = signal.historical_stats?.stats?.[signal.horizon];
  return (
    <div className="rounded-lg border border-edge bg-bg/60 p-3">
      <div className="flex items-center justify-between">
        <Link to={`/ticker/${signal.ticker}`} className="font-semibold text-accent">
          {signal.ticker}
        </Link>
        <span className={`text-sm font-bold ${labelColour(signal.label)}`}>
          {signal.label} {signed(signal.score)}
        </span>
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-muted">
        <SampleFlag flag={signal.sample_flag} n={signal.sample_size} />
        <span>confidence {num(signal.confidence)}</span>
        {stat && stat.n > 0 && (
          <span>
            median {signal.horizon} response {pct(stat.median)} (abn. {pct(stat.median_abnormal)})
          </span>
        )}
      </div>
      <WhyPanel signal={signal} />
    </div>
  );
}

export default function EventCard({ event, expanded = false }: { event: EventItem; expanded?: boolean }) {
  const analysis = event.analysis;
  const sentiment = analysis?.sentiment ?? null;

  return (
    <article className="card mb-3">
      <header className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2 text-xs text-muted">
            <span className="chip bg-edge text-gray-300">{event.source_key}</span>
            <span className="chip bg-edge text-gray-300">{titleCase(event.event_type)}</span>
            <time title={localTime(event.source_timestamp)}>
              {relativeTime(event.source_timestamp)}
            </time>
            {event.alert_triggered && (
              <span className="chip bg-accent/15 text-accent">alert triggered</span>
            )}
          </div>
          {event.title && <h3 className="mt-1.5 font-semibold leading-snug">{event.title}</h3>}
        </div>
      </header>

      <p className="mt-2 text-sm leading-relaxed text-gray-300">
        {expanded ? event.text : event.excerpt}
      </p>

      <div className="mt-3 flex flex-wrap items-center gap-2">
        {event.tickers.map((link) => (
          <Link
            key={link.ticker}
            to={`/ticker/${link.ticker}`}
            className={`chip ${confidenceChip(link.confidence)}`}
            title={`matched on "${link.matched_alias ?? '—'}" (${link.source})`}
          >
            {link.ticker} · {link.confidence}
          </Link>
        ))}
        {event.tickers.length === 0 && <span className="text-xs text-muted">no tickers matched</span>}
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-3 text-xs text-muted">
        <span>
          sentiment{' '}
          <span className={sentiment === null ? '' : sentiment > 0 ? 'text-bull' : 'text-bear'}>
            {signed(sentiment)}
          </span>
        </span>
        <span>
          analysis:{' '}
          <span className={event.analysis_status === 'COMPLETE' ? '' : 'text-warn'}>
            {event.analysis_status}
          </span>
          {analysis?.model ? ` (${analysis.model})` : ''}
        </span>
      </div>

      {event.signals.length > 0 && (
        <div className="mt-3 space-y-2">
          {event.signals.map((signal) => (
            <SignalRow key={signal.id} signal={signal} />
          ))}
        </div>
      )}

      {expanded && analysis && analysis.status === 'COMPLETE' && (
        <div className="mt-4 space-y-3 border-t border-edge pt-3 text-sm">
          {analysis.reasoning && (
            <div>
              <h4 className="label">Reading</h4>
              <p className="text-gray-300">{analysis.reasoning}</p>
            </div>
          )}
          {(
            [
              ['Facts stated in the source', analysis.facts],
              ['Stated positions', analysis.stated_positions],
              ['Third-party claims', analysis.third_party_claims],
              ['Speculation', analysis.speculation],
              ['Uncertainty', analysis.uncertainty],
            ] as const
          ).map(([heading, items]) =>
            items.length > 0 ? (
              <div key={heading}>
                <h4 className="label">{heading}</h4>
                <ul className="list-disc space-y-1 pl-4 text-gray-300">
                  {items.map((item) => (
                    <li key={item}>{item}</li>
                  ))}
                </ul>
              </div>
            ) : null,
          )}
        </div>
      )}

      <footer className="mt-3 flex flex-wrap items-center gap-3 text-xs">
        {!expanded && (
          <Link to={`/event/${event.id}`} className="font-medium text-accent">
            View analysis
          </Link>
        )}
        {event.tickers[0] && (
          <Link to={`/ticker/${event.tickers[0].ticker}`} className="font-medium text-accent">
            Set alert
          </Link>
        )}
        {event.url && (
          <a href={event.url} target="_blank" rel="noreferrer" className="text-muted underline">
            Source
          </a>
        )}
      </footer>
    </article>
  );
}
