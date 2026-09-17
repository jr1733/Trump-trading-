import { useCallback, useEffect, useState } from 'react';
import { NavLink, Navigate, Route, Routes, useLocation } from 'react-router-dom';

import Login from './pages/Login';
import Live from './pages/Live';
import EventDetail from './pages/EventDetail';
import Watchlist from './pages/Watchlist';
import Tickers from './pages/Tickers';
import TickerDetail from './pages/TickerDetail';
import Search from './pages/Search';
import Notifications from './pages/Notifications';
import Backtest from './pages/Backtest';
import DataQuality from './pages/DataQuality';
import Settings from './pages/Settings';
import { api, getToken } from './lib/api';

const NAV = [
  { to: '/', label: 'Live', icon: '◧' },
  { to: '/watchlist', label: 'Watch', icon: '★' },
  { to: '/tickers', label: 'Tickers', icon: '#' },
  { to: '/search', label: 'Search', icon: '⌕' },
  { to: '/notifications', label: 'Alerts', icon: '◔' },
  { to: '/backtest', label: 'Backtest', icon: '⟲' },
  { to: '/settings', label: 'More', icon: '⚙' },
];

export default function App() {
  const [authed, setAuthed] = useState(() => Boolean(getToken()));
  const [unread, setUnread] = useState(0);
  const location = useLocation();

  const refreshUnread = useCallback(async () => {
    if (!getToken()) return;
    try {
      const me = await api.get<{ unread_notifications: number }>('/me');
      setUnread(me.unread_notifications);
    } catch {
      /* the badge is cosmetic; a failure here must not break navigation */
    }
  }, []);

  useEffect(() => {
    void refreshUnread();
  }, [refreshUnread, location.pathname]);

  useEffect(() => {
    const timer = window.setInterval(refreshUnread, 60_000);
    return () => window.clearInterval(timer);
  }, [refreshUnread]);

  if (!authed) return <Login onSignedIn={() => setAuthed(true)} />;

  return (
    <div className="mx-auto flex min-h-dvh max-w-2xl flex-col">
      <header className="safe-top sticky top-0 z-10 border-b border-edge bg-bg/95 backdrop-blur">
        <div className="flex items-center justify-between px-4 py-3">
          <div>
            <h1 className="text-base font-bold leading-none">Event Market Intelligence</h1>
            <p className="mt-1 text-[11px] text-muted">Research tool · not investment advice</p>
          </div>
          <NavLink
            to="/notifications"
            className="relative rounded-lg border border-edge px-3 py-2 text-lg leading-none"
            aria-label={`Notifications${unread ? `, ${unread} unread` : ''}`}
          >
            🔔
            {unread > 0 && (
              <span className="absolute -right-1 -top-1 min-w-[18px] rounded-full bg-bear px-1 text-center text-[10px] font-bold leading-[18px] text-white">
                {unread > 99 ? '99+' : unread}
              </span>
            )}
          </NavLink>
        </div>
      </header>

      <main className="flex-1 px-4 pb-28 pt-4">
        <Routes>
          <Route path="/" element={<Live />} />
          <Route path="/event/:eventId" element={<EventDetail />} />
          <Route path="/watchlist" element={<Watchlist />} />
          <Route path="/tickers" element={<Tickers />} />
          <Route path="/ticker/:symbol" element={<TickerDetail />} />
          <Route path="/search" element={<Search />} />
          <Route path="/notifications" element={<Notifications onChange={refreshUnread} />} />
          <Route path="/backtest" element={<Backtest />} />
          <Route path="/data-quality" element={<DataQuality />} />
          <Route path="/settings" element={<Settings onSignedOut={() => setAuthed(false)} />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>

      <nav className="safe-bottom fixed inset-x-0 bottom-0 z-10 border-t border-edge bg-panel">
        <ul className="mx-auto flex max-w-2xl">
          {NAV.map((item) => (
            <li key={item.to} className="flex-1">
              <NavLink
                to={item.to}
                end={item.to === '/'}
                className={({ isActive }) =>
                  `flex flex-col items-center gap-0.5 py-2 text-[10px] ${
                    isActive ? 'text-accent' : 'text-muted'
                  }`
                }
              >
                <span className="text-base leading-none">{item.icon}</span>
                {item.label}
              </NavLink>
            </li>
          ))}
        </ul>
      </nav>
    </div>
  );
}
