import { FormEvent, useState } from 'react';

import { api, setToken, clearToken } from '../lib/api';

export default function Login({ onSignedIn }: { onSignedIn: () => void }) {
  const [token, setValue] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    setToken(token.trim());
    try {
      // Verify against a real authenticated endpoint rather than trusting input.
      await api.get('/me');
      onSignedIn();
    } catch {
      clearToken();
      setError('That token was not accepted.');
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mx-auto flex min-h-dvh max-w-sm flex-col justify-center px-6">
      <h1 className="text-xl font-bold">Event Market Intelligence</h1>
      <p className="mt-1 text-sm text-muted">
        Research and information tool. Not investment advice, and not connected to any brokerage.
      </p>

      <form onSubmit={submit} className="mt-6">
        <label className="label" htmlFor="token">
          Access token
        </label>
        <input
          id="token"
          className="input"
          type="password"
          autoComplete="current-password"
          value={token}
          onChange={(event) => setValue(event.target.value)}
          placeholder="APP_AUTH_TOKEN"
        />
        {error && <p className="mt-2 text-sm text-bear">{error}</p>}
        <button className="btn-primary mt-4 w-full" disabled={busy || !token.trim()}>
          {busy ? 'Checking…' : 'Sign in'}
        </button>
      </form>

      <p className="mt-6 text-[11px] leading-relaxed text-muted">
        The token is the <code>APP_AUTH_TOKEN</code> from your server environment. It is stored in
        this browser and sent in the Authorization header.
      </p>
    </div>
  );
}
