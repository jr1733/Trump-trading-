/* Similar events for one event.
 *
 * The header states which measure produced the scores. That is not decoration:
 * a lexical embedder and a transformer put "related" at completely different
 * numbers, so a bare 0.31 is meaningless without knowing which produced it.
 */

import { Link } from 'react-router-dom';

import { Empty, Spinner } from './ui';
import { localTime, num, pct, titleCase } from '../lib/format';
import { useApi } from '../lib/useApi';

interface SimilarItem {
  event_id: string;
  title: string;
  source: string;
  event_type: string;
  source_timestamp: string;
  similarity: number;
  is_historical: boolean;
  returns: Record<string, { raw_return: number; abnormal_return: number | null }>;
}

interface SimilarBody {
  event_id: string;
  ticker: string | null;
  provider: string;
  measure: string;
  semantic: boolean;
  threshold: number;
  count: number;
  items: SimilarItem[];
}

export default function SimilarEvents({ eventId }: { eventId: string }) {
  const { data, error, loading } = useApi<SimilarBody>(`/events/${eventId}/similar?limit=8`);

  if (loading) return <Spinner label="Finding similar events" />;
  if (error) return <p className="text-xs text-bear">{error}</p>;
  if (!data) return null;

  return (
    <div>
      <div className="mb-2 flex flex-wrap items-center gap-2 text-[11px] text-muted">
        <span className={`chip ${data.semantic ? 'bg-bull/15 text-bull' : 'bg-edge text-muted'}`}>
          {data.semantic ? 'semantic' : 'lexical'}
        </span>
        <span>{data.measure}</span>
        <span>· similarity ≥ {num(data.threshold)}</span>
      </div>

      {data.items.length === 0 ? (
        <Empty>
          No past event cleared the similarity threshold for this one. Only events that happened
          <em> before</em> this one are ever considered.
        </Empty>
      ) : (
        <div className="-mx-1 overflow-x-auto">
          <table className="w-full min-w-[460px] text-xs">
            <thead className="text-muted">
              <tr>
                <th className="py-1 pr-2 text-left font-medium">Date</th>
                <th className="py-1 pr-2 text-left font-medium">Event</th>
                <th className="py-1 pr-2 text-right font-medium">Sim.</th>
                <th className="py-1 pr-2 text-right font-medium">1d</th>
                <th className="py-1 text-right font-medium">5d</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((item) => {
                const oneDay = item.returns['1d']?.raw_return ?? null;
                const fiveDay = item.returns['5d']?.raw_return ?? null;
                return (
                  <tr key={item.event_id} className="border-t border-edge/60 align-top">
                    <td className="whitespace-nowrap py-1.5 pr-2 text-muted">
                      {localTime(item.source_timestamp)}
                    </td>
                    <td className="py-1.5 pr-2">
                      <Link to={`/event/${item.event_id}`} className="text-accent">
                        <span className="line-clamp-2">{item.title}</span>
                      </Link>
                      <span className="text-[10px] text-muted">
                        {titleCase(item.event_type)}
                        {item.is_historical ? ' · archive' : ''}
                      </span>
                    </td>
                    <td className="py-1.5 pr-2 text-right tabular-nums">{num(item.similarity)}</td>
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
          {data.ticker && (
            <p className="mt-2 text-[11px] text-muted">
              Returns are for {data.ticker} around each past event. A similar past event is not
              evidence that this one will be followed by the same move.
            </p>
          )}
        </div>
      )}
    </div>
  );
}
