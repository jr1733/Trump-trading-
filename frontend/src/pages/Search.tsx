import { FormEvent, useState } from 'react';

import EventCard from '../components/EventCard';
import { Disclaimer, Empty, ErrorBox, Spinner } from '../components/ui';
import { api, type EventItem } from '../lib/api';
import { pct } from '../lib/format';

interface Hit extends EventItem {
  subsequent_returns: Record<string, { raw_return: number; abnormal_return: number | null }>;
}

export default function Search() {
  const [query, setQuery] = useState('');
  const [submitted, setSubmitted] = useState('');
  const [items, setItems] = useState<Hit[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  async function run(event: FormEvent) {
    event.preventDefault();
    const trimmed = query.trim();
    if (trimmed.length < 2) return;
    setLoading(true);
    setError(null);
    setNotice(null);
    try {
      const body = await api.get<{ items: Hit[] }>(
        `/search?q=${encodeURIComponent(trimmed)}&limit=40`,
      );
      setItems(body.items);
      setSubmitted(trimmed);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  }

  async function createAlertFromSearch() {
    if (!submitted) return;
    try {
      await api.post('/alert-rules', {
        name: `Search: ${submitted}`,
        rule_type: 'new_event',
        keywords: [submitted],
        channels: ['in_app'],
        search_query: submitted,
      });
      setNotice(`Alert rule created for "${submitted}".`);
    } catch (err) {
      setError((err as Error).message);
    }
  }

  return (
    <div>
      <form onSubmit={run} className="mb-3 flex gap-2">
        <input
          className="input"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Search all events…"
          aria-label="Search query"
          type="search"
        />
        <button className="btn-primary shrink-0" disabled={loading}>
          Search
        </button>
      </form>

      {submitted && (
        <div className="mb-3 flex items-center justify-between gap-2">
          <p className="text-xs text-muted">
            {items?.length ?? 0} result{items?.length === 1 ? '' : 's'} for “{submitted}”
          </p>
          <button className="btn text-xs" onClick={createAlertFromSearch}>
            Create alert from this search
          </button>
        </div>
      )}
      {notice && <p className="mb-3 text-xs text-bull">{notice}</p>}

      {loading && <Spinner label="Searching" />}
      {error && <ErrorBox message={error} />}
      {items?.length === 0 && <Empty>No events matched that search.</Empty>}

      {items?.map((hit) => (
        <div key={hit.id}>
          <EventCard event={hit} />
          {Object.keys(hit.subsequent_returns).length > 0 && (
            <div className="-mt-2 mb-3 flex flex-wrap gap-3 rounded-b-xl border border-t-0 border-edge bg-bg/60 px-4 py-2 text-[11px] text-muted">
              <span>Subsequent returns:</span>
              {Object.entries(hit.subsequent_returns).map(([horizon, value]) => (
                <span key={horizon}>
                  {horizon}{' '}
                  <span className={value.raw_return >= 0 ? 'text-bull' : 'text-bear'}>
                    {pct(value.raw_return)}
                  </span>
                </span>
              ))}
            </div>
          )}
        </div>
      ))}

      <Disclaimer />
    </div>
  );
}
