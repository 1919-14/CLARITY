import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';

import App from './App.jsx';

describe('application shell', () => {
  it('renders the not-found page inside the accessible shell', () => {
    render(
      <MemoryRouter initialEntries={['/an-unknown-leaf']}>
        <App />
      </MemoryRouter>,
    );

    expect(screen.getByRole('heading', { name: /this leaf is missing/i })).toBeInTheDocument();
    expect(screen.getByRole('navigation', { name: /primary/i })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /skip to main content/i })).toHaveAttribute(
      'href',
      '#main-content',
    );
    expect(screen.getByRole('main')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /return to the dashboard/i })).toHaveAttribute('href', '/');
  });
});
