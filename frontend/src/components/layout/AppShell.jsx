import { Link, NavLink, Outlet } from 'react-router-dom';

import { API_BASE_URL } from '../../api/client.js';
import {
  BookIcon,
  ChartIcon,
  FlaskIcon,
  PlusIcon,
  ScrollIcon,
} from '../icons.jsx';

const NAV_ITEMS = [
  { to: '/', label: 'Dashboard', icon: BookIcon, end: true },
  { to: '/hypotheses', label: 'Hypotheses', icon: FlaskIcon },
  { to: '/reports', label: 'Reports', icon: ScrollIcon },
  { to: '/benchmarks', label: 'Benchmarks', icon: ChartIcon },
];

export function AppShell() {
  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">
        Skip to main content
      </a>
      <div className="atmosphere" aria-hidden="true" />

      <header className="site-header">
        <div className="site-header__inner">
          <Link to="/" className="wordmark" aria-label="CLARITY research dashboard">
            <span className="wordmark__name">CLARITY</span>
            <span className="wordmark__rule" aria-hidden="true" />
            <span className="wordmark__tagline">Autonomous Black-Box Scientist</span>
          </Link>

          <nav className="site-nav" aria-label="Primary">
            {NAV_ITEMS.map(({ to, label, icon: Icon, end }) => (
              <NavLink
                key={to}
                to={to}
                end={end}
                className={({ isActive }) => `nav-link${isActive ? ' is-active' : ''}`}
              >
                <Icon size={16} />
                <span>{label}</span>
              </NavLink>
            ))}
          </nav>

          <Link to="/runs/new" className="button button--primary site-header__action">
            <PlusIcon size={16} />
            <span>Create run</span>
          </Link>
        </div>
      </header>

      <main id="main-content" className="page" tabIndex={-1}>
        <Outlet />
      </main>

      <footer className="site-footer">
        <div className="site-footer__inner">
          <p className="site-footer__motto">
            Observation, hypothesis, experiment — recorded, never invented.
          </p>
          <p className="site-footer__meta">
            <span>API</span> <code>{API_BASE_URL}</code>
          </p>
        </div>
      </footer>
    </div>
  );
}
