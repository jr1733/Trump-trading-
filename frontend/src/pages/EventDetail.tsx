import { Link, useParams } from 'react-router-dom';

import EventCard from '../components/EventCard';
import SimilarEvents from '../components/SimilarEvents';
import { Disclaimer, ErrorBox, Section, Spinner } from '../components/ui';
import type { EventItem } from '../lib/api';
import { localTimeFull } from '../lib/format';
import { useApi } from '../lib/useApi';

export default function EventDetail() {
  const { eventId } = useParams();
  const { data, error, loading, reload } = useApi<EventItem>(eventId ? `/events/${eventId}` : null);

  if (loading) return <Spinner />;
  if (error) return <ErrorBox message={error} onRetry={reload} />;
  if (!data) return null;

  return (
    <div>
      <Link to="/" className="mb-3 inline-block text-sm text-accent">
        ← Back to live feed
      </Link>
      <EventCard event={data} expanded />

      <Section title="Similar past events">
        <div className="card">
          <SimilarEvents eventId={data.id} />
        </div>
      </Section>

      <div className="card mt-3 text-xs text-muted">
        <h3 className="label">Provenance</h3>
        <dl className="space-y-1">
          <div className="flex justify-between gap-4">
            <dt>Source published</dt>
            <dd className="text-right text-gray-300">{localTimeFull(data.source_timestamp)}</dd>
          </div>
          <div className="flex justify-between gap-4">
            <dt>Ingested</dt>
            <dd className="text-right text-gray-300">{localTimeFull(data.ingestion_timestamp)}</dd>
          </div>
          <div className="flex justify-between gap-4">
            <dt>Processed</dt>
            <dd className="text-right text-gray-300">
              {data.processing_timestamp ? localTimeFull(data.processing_timestamp) : '—'}
            </dd>
          </div>
          <div className="flex justify-between gap-4">
            <dt>Relevance score</dt>
            <dd className="text-right text-gray-300">{data.relevance_score.toFixed(2)}</dd>
          </div>
        </dl>
      </div>

      {data.analysis?.validation_error && (
        <div className="card mt-3 border-warn/40 text-xs text-warn">
          Model output failed validation: {data.analysis.validation_error}
        </div>
      )}

      <Disclaimer />
    </div>
  );
}
