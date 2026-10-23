import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '../api/client.js';
import { makeBudget, makeProgress, makeRun } from '../test/fixtures.js';

vi.mock('../api/runs.js', () => ({
  listRuns: vi.fn(),
  getRun: vi.fn(),
  getRunStatus: vi.fn(),
  getRunBudget: vi.fn(),
  cancelRun: vi.fn(),
  createRun: vi.fn(),
}));

import { cancelRun, getRun, getRunBudget, getRunStatus } from '../api/runs.js';
import { RunDetailsView } from './RunDetailsPage.jsx';

const RUN_ID = 'run-4c1b0e7a2d9f';

function renderRun(pollIntervalMs = 40) {
  return render(
    <MemoryRouter initialEntries={[`/runs/${RUN_ID}`]}>
      <Routes>
        <Route
          path="/runs/:runId"
          element={<RunDetailsView runId={RUN_ID} pollIntervalMs={pollIntervalMs} />}
        />
      </Routes>
    </MemoryRouter>,
  );
}

function mockRunningRun(overrides = {}) {
  getRun.mockResolvedValue({ run: makeRun({ run_id: RUN_ID, ...overrides }) });
  getRunStatus.mockResolvedValue({ status: makeProgress({ run_id: RUN_ID }) });
  getRunBudget.mockResolvedValue({ run_id: RUN_ID, budget: makeBudget({ run_id: RUN_ID }) });
}

describe('RunDetailsView', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders lifecycle state, phase and resource accounting from the API', async () => {
    mockRunningRun();
    renderRun();

    expect(await screen.findByRole('heading', { name: 'Nocturnal growth response' })).toBeInTheDocument();
    expect(screen.getByText('Candidate evaluation')).toBeInTheDocument();
    expect(screen.getByText('Fitting candidate 5 of 10')).toBeInTheDocument();
    expect(screen.getByText('2 / 10')).toBeInTheDocument();
    expect(screen.getByText('5 / 100')).toBeInTheDocument();
    expect(screen.getByText(/application-side estimate/i)).toBeInTheDocument();
    expect(screen.getAllByText(/no provider-enforced spending cap/i).length).toBeGreaterThan(0);
  });

  it('refreshes while the run is active and uses one polling loop', async () => {
    mockRunningRun();
    renderRun();

    await screen.findByRole('heading', { name: 'Nocturnal growth response' });

    await waitFor(() => {
      expect(getRun.mock.calls.length).toBeGreaterThan(1);
    });

    // Each poll is one batch of run + status + budget, never overlapping loops.
    expect(getRunStatus.mock.calls.length).toBe(getRun.mock.calls.length);
    expect(getRunBudget.mock.calls.length).toBe(getRun.mock.calls.length);
    expect(getRun.mock.calls.length).toBeLessThan(6);
  });

  it('stops polling once the backend reports a terminal state', async () => {
    getRun.mockResolvedValue({
      run: makeRun({ run_id: RUN_ID, status: 'completed', completed_at: '2026-10-24T09:00:00+00:00' }),
    });
    getRunStatus.mockResolvedValue({
      status: makeProgress({ run_id: RUN_ID, status: 'completed', phase: 'completed' }),
    });
    getRunBudget.mockResolvedValue({ run_id: RUN_ID, budget: makeBudget({ run_id: RUN_ID }) });

    renderRun();

    await screen.findByRole('heading', { name: 'Nocturnal growth response' });
    await waitFor(() => {
      expect(getRun).toHaveBeenCalledTimes(1);
    });

    await new Promise((resolve) => setTimeout(resolve, 140));
    expect(getRun).toHaveBeenCalledTimes(1);
    expect(screen.getByText(/polling stopped/i)).toBeInTheDocument();
  });

  it('keeps the last known state and marks it stale when a refresh fails', async () => {
    getRun
      .mockResolvedValueOnce({ run: makeRun({ run_id: RUN_ID }) })
      .mockRejectedValue(new ApiError('The backend is unavailable.', { kind: 'network' }));
    getRunStatus.mockResolvedValue({ status: makeProgress({ run_id: RUN_ID }) });
    getRunBudget.mockResolvedValue({ run_id: RUN_ID, budget: makeBudget({ run_id: RUN_ID }) });

    renderRun();

    expect(await screen.findByRole('heading', { name: 'Nocturnal growth response' })).toBeInTheDocument();
    expect(await screen.findByText('Showing the last known state')).toBeInTheDocument();
    expect(screen.getByText(/the backend is unavailable/i)).toBeInTheDocument();
    expect(screen.getAllByText('Nocturnal growth response').length).toBeGreaterThan(0);
  });

  it('offers cancellation only for a cancellable run and confirms through the API', async () => {
    mockRunningRun();
    cancelRun.mockResolvedValue({
      message: 'Run cancellation requested. Shutdown of in-flight work is in progress.',
    });

    renderRun();
    await screen.findByRole('heading', { name: 'Nocturnal growth response' });

    await userEvent.click(screen.getByRole('button', { name: /cancel run/i }));
    const dialog = screen.getByRole('dialog');
    expect(within(dialog).getByText(/cancel “nocturnal growth response”/i)).toBeInTheDocument();

    await userEvent.click(within(dialog).getByRole('button', { name: /request cancellation/i }));

    await waitFor(() => {
      expect(cancelRun).toHaveBeenCalledWith(RUN_ID);
    });
    expect(await screen.findByText(/run cancellation requested/i)).toBeInTheDocument();
    expect(getRun.mock.calls.length).toBeGreaterThan(1);
  });

  it('reports a cancellation conflict without pretending the run stopped', async () => {
    mockRunningRun();
    cancelRun.mockRejectedValue(
      new ApiError('Run is already in a terminal state and cannot be cancelled.', { status: 409 }),
    );

    renderRun();
    await screen.findByRole('heading', { name: 'Nocturnal growth response' });

    await userEvent.click(screen.getByRole('button', { name: /cancel run/i }));
    await userEvent.click(screen.getByRole('button', { name: /request cancellation/i }));

    expect(await screen.findByText('Cancellation was not accepted')).toBeInTheDocument();
    expect(screen.getByText(/already reached a terminal state/i)).toBeInTheDocument();
  });

  it('does not offer cancellation for a completed run', async () => {
    getRun.mockResolvedValue({ run: makeRun({ run_id: RUN_ID, status: 'completed' }) });
    getRunStatus.mockResolvedValue({
      status: makeProgress({ run_id: RUN_ID, status: 'completed', phase: 'completed' }),
    });
    getRunBudget.mockResolvedValue({ run_id: RUN_ID, budget: makeBudget({ run_id: RUN_ID }) });

    renderRun();

    await screen.findByRole('heading', { name: 'Nocturnal growth response' });
    expect(screen.queryByRole('button', { name: /cancel run/i })).not.toBeInTheDocument();
  });

  it('explains a missing run instead of showing fabricated state', async () => {
    getRun.mockRejectedValue(new ApiError("Run 'run-missing' not found.", { status: 404 }));
    getRunStatus.mockRejectedValue(new ApiError("Run 'run-missing' not found.", { status: 404 }));
    getRunBudget.mockRejectedValue(new ApiError("Run 'run-missing' not found.", { status: 404 }));

    renderRun();

    expect(await screen.findByText('This run does not exist')).toBeInTheDocument();
    expect(screen.getByText(/no persisted run with id/i)).toBeInTheDocument();
  });

  it('renders an unknown future lifecycle state without crashing', async () => {
    getRun.mockResolvedValue({ run: makeRun({ run_id: RUN_ID, status: 'teleported' }) });
    getRunStatus.mockResolvedValue({
      status: makeProgress({ run_id: RUN_ID, status: 'teleported', phase: 'future_phase' }),
    });
    getRunBudget.mockResolvedValue({ run_id: RUN_ID, budget: makeBudget({ run_id: RUN_ID }) });

    renderRun();

    expect((await screen.findAllByText('Teleported')).length).toBeGreaterThan(0);
    expect(screen.getByText('Future Phase')).toBeInTheDocument();
  });
});
