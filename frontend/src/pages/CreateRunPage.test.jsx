import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes, useParams } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '../api/client.js';

vi.mock('../api/runs.js', () => ({
  createRun: vi.fn(),
  listRuns: vi.fn(),
  getRun: vi.fn(),
  getRunStatus: vi.fn(),
  getRunBudget: vi.fn(),
  cancelRun: vi.fn(),
}));

import { createRun } from '../api/runs.js';
import { CreateRunPage } from './CreateRunPage.jsx';

function RunStub() {
  const { runId } = useParams();
  return <h1>Run details {runId}</h1>;
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/runs/new']}>
      <Routes>
        <Route path="/runs/new" element={<CreateRunPage />} />
        <Route path="/runs/:runId" element={<RunStub />} />
        <Route path="/" element={<h1>Dashboard home</h1>} />
      </Routes>
    </MemoryRouter>,
  );
}

async function fillName(name = 'Nocturnal growth response') {
  const input = screen.getByLabelText(/run name/i);
  await userEvent.clear(input);
  await userEvent.type(input, name);
}

describe('CreateRunPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('shows the backend defaults in the configuration form', () => {
    renderPage();

    expect(screen.getByLabelText(/oracle query budget/i)).toHaveValue('10');
    expect(screen.getByLabelText(/candidate evaluation limit/i)).toHaveValue('100');
    expect(screen.getByLabelText(/maximum concurrent tasks/i)).toHaveValue('4');
    expect(screen.getByLabelText(/maximum run duration/i)).toHaveValue('3600');
    expect(screen.getByLabelText(/compute spending limit/i)).toHaveValue('');
    expect(screen.getByLabelText(/execution mode/i)).toHaveValue('local');
    expect(screen.getByText(/not a provider-enforced cap/i)).toBeInTheDocument();
  });

  it('rejects an empty name with a plain-language error and no request', async () => {
    renderPage();

    await userEvent.click(screen.getByRole('button', { name: /create discovery run/i }));

    expect(
      screen.getAllByText(/run name is required so the run can be identified/i).length,
    ).toBeGreaterThan(0);
    expect(createRun).not.toHaveBeenCalled();
  });

  it('rejects negative budgets before submission', async () => {
    renderPage();
    await fillName();

    const queries = screen.getByLabelText(/oracle query budget/i);
    await userEvent.clear(queries);
    await userEvent.type(queries, '-5');
    await userEvent.click(screen.getByRole('button', { name: /create discovery run/i }));

    expect(screen.getAllByText(/oracle query budget cannot be negative/i).length).toBeGreaterThan(0);
    expect(createRun).not.toHaveBeenCalled();
  });

  it('submits a valid configuration and shows the returned run id', async () => {
    createRun.mockResolvedValue({ run: { run_id: 'run-created-1', status: 'queued' } });

    renderPage();
    await fillName('Nocturnal growth response');
    await userEvent.type(screen.getByLabelText(/compute spending limit/i), '5.50');
    await userEvent.click(screen.getByRole('button', { name: /create discovery run/i }));

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: /run details run-created-1/i })).toBeInTheDocument();
    });

    expect(createRun).toHaveBeenCalledTimes(1);
    expect(createRun).toHaveBeenCalledWith(
      {
        name: 'Nocturnal growth response',
        domain: 'default',
        execution_mode: 'local',
        budget: {
          max_oracle_queries: 10,
          max_candidate_evaluations: 100,
          max_concurrent_remote_tasks: 4,
          max_runtime_seconds: 3600,
          max_compute_spend_usd: 5.5,
        },
      },
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
  });

  it('prevents duplicate submissions while the request is pending', async () => {
    createRun.mockReturnValue(new Promise(() => {}));

    renderPage();
    await fillName();

    const submit = screen.getByRole('button', { name: /create discovery run/i });
    await userEvent.click(submit);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /creating run/i })).toBeDisabled();
    });
    await userEvent.click(screen.getByRole('button', { name: /creating run/i }));
    expect(createRun).toHaveBeenCalledTimes(1);
  });

  it('shows backend validation errors and keeps the entered values', async () => {
    createRun.mockRejectedValue(
      new ApiError('Invalid run budget configuration.', {
        status: 400,
        code: 'Bad Request',
        details: [{ field: 'max_oracle_queries', code: 'OUT_OF_RANGE', message: 'must be <= 100000' }],
      }),
    );

    renderPage();
    await fillName('Kept name');
    await userEvent.click(screen.getByRole('button', { name: /create discovery run/i }));

    expect(await screen.findByText('The run was not created')).toBeInTheDocument();
    expect(screen.getByText(/must be <= 100000/)).toBeInTheDocument();
    expect(screen.getByLabelText(/run name/i)).toHaveValue('Kept name');
    expect(screen.getByLabelText(/oracle query budget/i)).toHaveValue('10');
  });

  it('does not retry blindly when creation times out', async () => {
    createRun.mockRejectedValue(
      new ApiError('The CLARITY backend did not respond within 15 seconds.', { kind: 'timeout' }),
    );

    renderPage();
    await fillName();
    await userEvent.click(screen.getByRole('button', { name: /create discovery run/i }));

    expect(await screen.findByText('Submission outcome unknown')).toBeInTheDocument();
    expect(screen.getByText(/a run may or may not have been created/i)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /check recent runs before trying again/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /create discovery run/i })).toBeDisabled();
    expect(createRun).toHaveBeenCalledTimes(1);
  });
});
