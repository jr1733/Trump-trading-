import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';

import { Empty, ErrorBox, Section, Spinner, Stat } from '../components/ui';
import { api, clearToken, type AppConfig } from '../lib/api';
import { num, titleCase } from '../lib/format';
import { useApi } from '../lib/useApi';

interface Preferences {
  enabled: boolean;
  quiet_hours_start: number | null;
  quiet_hours_end: number | null;
  timezone: string;
  channels: string[];
  min_confidence: number;
  min_sample_size: number;
  max_per_hour: number;
  digest_enabled: boolean;
  digest_hour_local: number;
  in_quiet_hours_now: boolean;
}

interface PushStatus {
  public_key: string | null;
  configured: boolean;
  phase: string;
  subscriptions: Array<{ id: string; endpoint: string; active: boolean; failure_count: number }>;
}

interface SourceRow {
  key: string;
  name: string;
  kind: string;
  enabled: boolean;
  priority: string;
  poll_interval_minutes: number;
  status: string;
  consecutive_failures: number;
  last_error: string | null;
}

/** Standalone = launched from the Home Screen. On iOS this is the only mode in
 *  which Web Push works at all, so we detect and say so explicitly. */
function isStandalone(): boolean {
  return (
    window.matchMedia('(display-mode: standalone)').matches ||
    // iOS Safari exposes this non-standard flag on navigator.
    (window.navigator as unknown as { standalone?: boolean }).standalone === true
  );
}

function isIOS(): boolean {
  return /iphone|ipad|ipod/i.test(window.navigator.userAgent);
}

/** VAPID public keys are URL-safe base64; the Push API wants raw bytes. */
function urlBase64ToBytes(base64: string): ArrayBuffer {
  const padded = (base64 + '='.repeat((4 - (base64.length % 4)) % 4))
    .replace(/-/g, '+')
    .replace(/_/g, '/');
  const raw = window.atob(padded);
  const buffer = new ArrayBuffer(raw.length);
  const view = new Uint8Array(buffer);
  for (let i = 0; i < raw.length; i += 1) view[i] = raw.charCodeAt(i);
  return buffer;
}

export default function Settings({ onSignedOut }: { onSignedOut: () => void }) {
  const config = useApi<AppConfig>('/config');
  const prefs = useApi<Preferences>('/preferences');
  const push = useApi<PushStatus>('/push/status');
  const sources = useApi<{ items: SourceRow[] }>('/sources');

  const [draft, setDraft] = useState<Preferences | null>(null);
  const [permission, setPermission] = useState<string>(
    typeof Notification === 'undefined' ? 'unsupported' : Notification.permission,
  );
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (prefs.data) setDraft(prefs.data);
  }, [prefs.data]);

  async function savePrefs() {
    if (!draft) return;
    setBusy(true);
    try {
      await api.put('/preferences', draft);
      setMessage('Preferences saved.');
      prefs.reload();
    } catch (error) {
      setMessage((error as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function enablePush() {
    setMessage(null);
    if (typeof Notification === 'undefined') {
      setMessage('This browser does not support notifications.');
      return;
    }
    if (isIOS() && !isStandalone()) {
      setMessage(
        'On iPhone, notifications only work once this app is added to the Home Screen. ' +
          'Tap Share → Add to Home Screen, then open it from the icon and try again.',
      );
      return;
    }
    // Must be inside the tap handler: iOS rejects a permission prompt otherwise.
    const result = await Notification.requestPermission();
    setPermission(result);
    if (result !== 'granted') {
      setMessage('Notification permission was not granted. In-app notifications still work.');
      return;
    }
    if (!push.data?.public_key) {
      setMessage(
        'Permission granted, but the server has no VAPID keys, so nothing can be pushed. ' +
          'Generate a keypair (scripts/generate_vapid_keys.py) and set WEB_PUSH_PUBLIC_KEY / ' +
          'WEB_PUSH_PRIVATE_KEY. The notification centre keeps working meanwhile.',
      );
      return;
    }
    try {
      const registration = await navigator.serviceWorker.ready;
      const subscription = await registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToBytes(push.data.public_key),
      });
      const json = subscription.toJSON() as { endpoint?: string; keys?: Record<string, string> };
      await api.post('/push/subscribe', {
        endpoint: json.endpoint,
        keys: json.keys,
        user_agent: navigator.userAgent,
      });
      setMessage('Push subscription registered.');
      push.reload();
    } catch (error) {
      setMessage(`Could not subscribe: ${(error as Error).message}`);
    }
  }

  async function sendTest() {
    setBusy(true);
    try {
      const result = await api.post<{ created: boolean; reason: string | null }>(
        '/notifications/test',
      );
      setMessage(
        result.created
          ? 'Test notification created — check the notification centre.'
          : `Suppressed: ${result.reason}`,
      );
    } finally {
      setBusy(false);
    }
  }

  async function runOperation(path: string, label: string) {
    setBusy(true);
    setMessage(`${label}…`);
    try {
      const result = await api.post<Record<string, unknown>>(path);
      setMessage(`${label}: ${JSON.stringify(result).slice(0, 200)}`);
      sources.reload();
    } catch (error) {
      setMessage(`${label} failed: ${(error as Error).message}`);
    } finally {
      setBusy(false);
    }
  }

  function signOut() {
    clearToken();
    onSignedOut();
  }

  return (
    <div>
      {message && <div className="card mb-3 border-accent/40 text-sm">{message}</div>}

      <Section title="Notifications">
        {prefs.loading && <Spinner />}
        {prefs.error && <ErrorBox message={prefs.error} onRetry={prefs.reload} />}
        {draft && (
          <div className="card space-y-3">
            <div className="grid grid-cols-2 gap-2">
              <Stat label="Browser permission" value={permission} />
              <Stat
                label="Installed to Home Screen"
                value={isStandalone() ? 'yes' : 'no'}
                tone={isStandalone() ? 'text-bull' : 'text-warn'}
              />
              <Stat
                label="Push configured"
                value={push.data?.configured ? 'yes' : 'no'}
                tone={push.data?.configured ? 'text-bull' : 'text-warn'}
              />
              <Stat
                label="Email configured"
                value={config.data?.email_configured ? 'yes' : 'no'}
                tone={config.data?.email_configured ? 'text-bull' : 'text-muted'}
              />
            </div>

            {isIOS() && !isStandalone() && (
              <p className="rounded-lg border border-warn/40 bg-warn/5 p-2 text-xs text-warn">
                On iPhone, Web Push requires the app to be added to the Home Screen: tap Share →
                Add to Home Screen, then open it from the icon. In a Safari tab, push cannot be
                delivered at all.
              </p>
            )}

            <div className="flex flex-wrap gap-2">
              <button className="btn-primary" onClick={enablePush}>
                Enable notifications
              </button>
              <button className="btn" onClick={sendTest} disabled={busy}>
                Send test notification
              </button>
            </div>

            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="label" htmlFor="quiet-start">
                  Quiet hours start
                </label>
                <input
                  id="quiet-start"
                  className="input"
                  type="number"
                  min={0}
                  max={23}
                  value={draft.quiet_hours_start ?? ''}
                  onChange={(event) =>
                    setDraft({
                      ...draft,
                      quiet_hours_start: event.target.value === '' ? null : Number(event.target.value),
                    })
                  }
                />
              </div>
              <div>
                <label className="label" htmlFor="quiet-end">
                  Quiet hours end
                </label>
                <input
                  id="quiet-end"
                  className="input"
                  type="number"
                  min={0}
                  max={23}
                  value={draft.quiet_hours_end ?? ''}
                  onChange={(event) =>
                    setDraft({
                      ...draft,
                      quiet_hours_end: event.target.value === '' ? null : Number(event.target.value),
                    })
                  }
                />
              </div>
              <div className="col-span-2">
                <label className="label" htmlFor="tz">
                  Timezone
                </label>
                <input
                  id="tz"
                  className="input"
                  value={draft.timezone}
                  onChange={(event) => setDraft({ ...draft, timezone: event.target.value })}
                />
              </div>
              <div>
                <label className="label" htmlFor="min-conf">
                  Min confidence
                </label>
                <input
                  id="min-conf"
                  className="input"
                  type="number"
                  step="0.05"
                  min={0}
                  max={1}
                  value={draft.min_confidence}
                  onChange={(event) =>
                    setDraft({ ...draft, min_confidence: Number(event.target.value) })
                  }
                />
              </div>
              <div>
                <label className="label" htmlFor="min-n">
                  Min sample size
                </label>
                <input
                  id="min-n"
                  className="input"
                  type="number"
                  min={0}
                  value={draft.min_sample_size}
                  onChange={(event) =>
                    setDraft({ ...draft, min_sample_size: Number(event.target.value) })
                  }
                />
              </div>
              <div>
                <label className="label" htmlFor="max-hour">
                  Max per hour
                </label>
                <input
                  id="max-hour"
                  className="input"
                  type="number"
                  min={1}
                  value={draft.max_per_hour}
                  onChange={(event) =>
                    setDraft({ ...draft, max_per_hour: Number(event.target.value) })
                  }
                />
              </div>
              <div>
                <label className="label" htmlFor="digest-hour">
                  Digest hour (local)
                </label>
                <input
                  id="digest-hour"
                  className="input"
                  type="number"
                  min={0}
                  max={23}
                  value={draft.digest_hour_local}
                  onChange={(event) =>
                    setDraft({ ...draft, digest_hour_local: Number(event.target.value) })
                  }
                />
              </div>
            </div>

            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={draft.enabled}
                onChange={(event) => setDraft({ ...draft, enabled: event.target.checked })}
              />
              Notifications enabled
            </label>

            <button className="btn-primary" onClick={savePrefs} disabled={busy}>
              Save preferences
            </button>
            {prefs.data?.in_quiet_hours_now && (
              <p className="text-xs text-warn">
                Quiet hours are active right now — alerts are being suppressed (system alerts are
                not).
              </p>
            )}
          </div>
        )}
      </Section>

      <Section title="Push subscriptions">
        {push.data?.subscriptions.length === 0 ? (
          <Empty>{push.data.phase}</Empty>
        ) : (
          <ul className="space-y-2">
            {push.data?.subscriptions.map((sub) => (
              <li key={sub.id} className="card text-xs">
                <div className="truncate text-gray-300">{sub.endpoint}</div>
                <div className="mt-1 text-muted">
                  {sub.active ? 'active' : 'expired'} · failures {sub.failure_count}
                </div>
              </li>
            ))}
          </ul>
        )}
      </Section>

      <Section title="Sources">
        {sources.data?.items.map((row) => (
          <div key={row.key} className="card mb-2 text-xs">
            <div className="flex items-center justify-between">
              <span className="font-medium text-gray-200">{row.name}</span>
              <span
                className={
                  row.status === 'ONLINE'
                    ? 'text-bull'
                    : row.status === 'ERROR'
                      ? 'text-bear'
                      : 'text-warn'
                }
              >
                {row.status}
              </span>
            </div>
            <div className="mt-1 text-muted">
              {row.kind} · {row.priority} priority · every {row.poll_interval_minutes}m
              {row.consecutive_failures > 0 && ` · ${row.consecutive_failures} consecutive failures`}
            </div>
            {row.last_error && <div className="mt-1 text-bear">{row.last_error.slice(0, 160)}</div>}
          </div>
        ))}
      </Section>

      <Section title="Signal configuration">
        {config.data && (
          <div className="card text-xs">
            <p className="mb-2 text-muted">
              Weights are renormalised over whichever components are active for a given event.
            </p>
            <ul className="space-y-1">
              {Object.entries(config.data.signal_weights).map(([name, weight]) => (
                <li key={name} className="flex justify-between">
                  <span>{titleCase(name)}</span>
                  <span className="tabular-nums text-gray-300">{num(weight)}</span>
                </li>
              ))}
            </ul>
            <p className="mt-3 text-muted">
              Return scale: a median abnormal move of {(config.data.signal_return_scale * 100).toFixed(1)}%
              maps to a full-scale historical component. Sample gates: unreliable below{' '}
              {config.data.sample_size_gates.unreliable_below}, limited below{' '}
              {config.data.sample_size_gates.limited_below}. Primary horizon:{' '}
              {config.data.primary_horizon}.
            </p>
            <p className="mt-2 text-muted">
              Market data: {config.data.market_provider}
              {config.data.supports_intraday ? ' (intraday available)' : ' (daily bars only)'} ·
              analysis: {config.data.llm_mode}
            </p>
            <p className="mt-2 text-muted">
              Similarity: {config.data.embedding_label}
              {config.data.embedding_semantic
                ? ''
                : ' — install requirements-embeddings.txt and set ' +
                  'EMBEDDING_PROVIDER=sentence-transformers for semantic matching.'}
            </p>
          </div>
        )}
      </Section>

      <Section title="Operations">
        <div className="card flex flex-wrap gap-2">
          <button className="btn" disabled={busy} onClick={() => runOperation('/admin/poll', 'Poll sources')}>
            Poll sources
          </button>
          <button
            className="btn"
            disabled={busy}
            onClick={() => runOperation('/admin/pipeline', 'Run pipeline')}
          >
            Run pipeline
          </button>
          <button className="btn" disabled={busy} onClick={() => runOperation('/admin/seed', 'Seed data')}>
            Seed + load archive
          </button>
          <button className="btn" disabled={busy} onClick={() => runOperation('/admin/embed', 'Embed events')}>
            Embed events
          </button>
          <button
            className="btn"
            disabled={busy}
            onClick={() => runOperation('/admin/push-retry', 'Retry push')}
          >
            Retry failed push
          </button>
        </div>
      </Section>

      <Section title="Digest">
        <div className="card space-y-2">
          <p className="text-xs text-muted">
            The digest is delivered at your local digest hour, in the timezone set above. It goes
            to the notification centre first; email and push are additional channels you can
            enable under Notifications.
          </p>
          <div className="flex flex-wrap gap-2">
            <button
              className="btn"
              disabled={busy}
              onClick={() => runOperation('/digest/send', 'Send digest now')}
            >
              Send digest now
            </button>
            <Link to="/data-quality" className="btn">
              Data quality &amp; cost
            </Link>
          </div>
        </div>
      </Section>

      <Section title="Account">
        <button className="btn w-full" onClick={signOut}>
          Sign out
        </button>
      </Section>

      <p className="px-1 py-4 text-[11px] leading-relaxed text-muted">
        This is a research and information tool. It has no brokerage connection, places no orders,
        and takes no autonomous action.
      </p>
    </div>
  );
}
