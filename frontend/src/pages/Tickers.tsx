import { Link } from 'react-router-dom';

import { ErrorBox, Spinner } from '../components/ui';
import { useApi } from '../lib/useApi';

interface TickerRow {
  symbol: string;
  name: string;
  asset_class: string;
  sector: string | null;
  sector_etf: string | null;
  event_count: number;
}

export default function Tickers() {
  const { data, error, loading, reload } = useApi<{ items: TickerRow[] }>('/tickers');

  if (loading) return <Spinner />;
  if (error) return <ErrorBox message={error} onRetry={reload} />;

  return (
    <div>
      <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-muted">
        Tracked instruments
      </h2>
      <ul className="space-y-2">
        {data?.items.map((row) => (
          <li key={row.symbol}>
            <Link to={`/ticker/${row.symbol}`} className="card flex items-center justify-between">
              <div className="min-w-0">
                <div className="font-semibold">{row.symbol}</div>
                <div className="truncate text-xs text-muted">{row.name}</div>
              </div>
              <div className="shrink-0 text-right text-xs text-muted">
                <div>{row.sector ?? row.asset_class}</div>
                <div>{row.event_count} events</div>
              </div>
            </Link>
          </li>
        ))}
      </ul>
    </div>
  );
}
