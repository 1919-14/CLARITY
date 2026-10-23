import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { makeBudget } from '../../test/fixtures.js';
import { BudgetPanel } from './BudgetPanel.jsx';

describe('BudgetPanel', () => {
  it('shows missing monetary data as unavailable, never as zero', () => {
    render(<BudgetPanel budget={makeBudget()} />);

    const estimated = screen.getByText('Estimated cost').closest('.stat');
    expect(within(estimated).getByText('Unavailable')).toBeInTheDocument();
    expect(screen.queryByText('$0.0000')).not.toBeInTheDocument();
    expect(screen.getAllByText(/no provider-enforced spending cap is available/i).length).toBeGreaterThan(0);
  });

  it('shows a real zero as zero', () => {
    render(
      <BudgetPanel
        budget={makeBudget({ cost: { estimated_cost_usd: 0, settled_cost_usd: 0, data_state: 'settled' } })}
      />,
    );

    expect(screen.getAllByText('$0.0000')).toHaveLength(2);
  });

  it('reports application-enforced limits with used, limit and remaining values', () => {
    render(<BudgetPanel budget={makeBudget()} />);

    expect(screen.getByText('2 / 10')).toBeInTheDocument();
    expect(screen.getByText('remaining 8')).toBeInTheDocument();
    expect(screen.getByText('5 / 100')).toBeInTheDocument();
    expect(screen.getByText('Application-enforced limits')).toBeInTheDocument();
    expect(screen.getByText(/not recalculated in the browser/i)).toBeInTheDocument();
  });

  it('does not describe application limits as a provider cap', () => {
    render(<BudgetPanel budget={makeBudget()} />);
    expect(screen.queryByText(/provider-enforced spending cap exists/i)).not.toBeInTheDocument();
    expect(screen.getByText(/enforced by the application against its own estimates/i)).toBeInTheDocument();
  });

  it('surfaces exhaustion with the backend stop reason and preserves the honest framing', () => {
    render(
      <BudgetPanel
        budget={makeBudget({ is_exhausted: true, stop_reason: 'query_budget_exhausted' })}
      />,
    );

    expect(screen.getByText(/Work stopped: Query Budget Exhausted/)).toBeInTheDocument();
    expect(screen.getByText(/Resource exhaustion ended this run/i)).toBeInTheDocument();
  });

  it('handles a missing budget record without fabricating numbers', () => {
    render(<BudgetPanel budget={null} />);
    expect(screen.getByText(/Budget information is not available/i)).toBeInTheDocument();
  });
});
