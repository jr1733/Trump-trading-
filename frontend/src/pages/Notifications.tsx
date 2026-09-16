import { Link } from 'react-router-dom';

import { Empty, ErrorBox, Spinner } from '../components/ui';
import { api, type NotificationItem } from '../lib/api';
import { localTime, titleCase } from '../lib/format';
import { useApi } from '../lib/useApi';

function severityTone(severity: string): string {
  if (severity === 'critical') return 'border-bear/50';
  if (severity === 'warning') return 'border-warn/50';
  return 'border-edge';
}

export default function Notifications({ onChange }: { onChange: () => void }) {
  const { data, error, loading, reload } = useApi<{
    unread_count: number;
    items: NotificationItem[];
  }>('/notifications?limit=100');

  async function markRead(id: string) {
    await api.post(`/notifications/${id}/read`);
    reload();
    onChange();
  }

  async function markAll() {
    await api.post('/notifications/read-all');
    reload();
    onChange();
  }

  async function archive(id: string) {
    await api.post(`/notifications/${id}/archive`);
    reload();
    onChange();
  }

  async function remove(id: string) {
    await api.del(`/notifications/${id}`);
    reload();
    onChange();
  }

  return (
    <div>
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-muted">
          Notification centre {data ? `(${data.unread_count} unread)` : ''}
        </h2>
        <button className="btn text-xs" onClick={markAll} disabled={!data?.unread_count}>
          Mark all read
        </button>
      </div>

      {loading && <Spinner />}
      {error && <ErrorBox message={error} onRetry={reload} />}
      {data?.items.length === 0 && <Empty>No notifications yet.</Empty>}

      <ul className="space-y-2">
        {data?.items.map((note) => {
          const push = note.deliveries.find((d) => d.channel === 'web_push');
          return (
            <li
              key={note.id}
              className={`card ${severityTone(note.severity)} ${note.read_at ? 'opacity-70' : ''}`}
            >
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2 text-[11px] text-muted">
                    <span className="chip bg-edge text-gray-300">
                      {titleCase(note.notification_type)}
                    </span>
                    {!note.read_at && <span className="chip bg-accent/15 text-accent">unread</span>}
                    <time>{localTime(note.created_at)}</time>
                  </div>
                  <h3 className="mt-1 font-semibold">{note.title}</h3>
                  <p className="mt-1 text-sm leading-relaxed text-gray-300">{note.body}</p>
                </div>
              </div>

              <div className="mt-2 flex flex-wrap items-center gap-3 text-xs">
                {note.event_id && (
                  <Link to={`/event/${note.event_id}`} className="font-medium text-accent">
                    Review analysis
                  </Link>
                )}
                {note.ticker && (
                  <Link to={`/ticker/${note.ticker}`} className="font-medium text-accent">
                    {note.ticker}
                  </Link>
                )}
                {!note.read_at && (
                  <button className="text-muted underline" onClick={() => markRead(note.id)}>
                    Mark read
                  </button>
                )}
                <button className="text-muted underline" onClick={() => archive(note.id)}>
                  Archive
                </button>
                <button className="text-muted underline" onClick={() => remove(note.id)}>
                  Delete
                </button>
              </div>

              {push && (
                <p className="mt-2 text-[11px] text-muted">
                  push: {push.status.toLowerCase()}
                  {push.provider_response ? ` — ${push.provider_response}` : ''}
                </p>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
