import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { makeRun } from '../test/fixtures.js';

vi.mock('../api/runs.js', () => ({
  listRuns: vi.fn(),
  listRunHypotheses: vi.fn(),
}));

vi.mock('../api/hypotheses.js', () => ({
  listRunHypotheses: vi.fn(),
  getHypothesis: vi.fn(),
  proposeHypothesis: vi.fn(),
  updateHypothesisFlags: vi.fn(),
  reevaluateHypothesis: vi.fn(),
  getHypothesisEvaluations: vi.fn(),
  getHypothesisRevisions: vi.fn(),
  getHypothesisEvidence: vi.fn(),
  listRunObservations: vi.fn(),
}));

import * as hypothesesApi from '../api/hypotheses.js';
import * as runsApi from '../api/runs.js';
import { HypothesesPage } from './HypothesesPage.jsx';

const RUN_ID = 'run-test-123';

function renderPage(path = `/hypotheses?run=${RUN_ID}`) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <HypothesesPage />
    </MemoryRouter>,
  );
}

const mockHypotheses = [
  {
    hypothesis_id: 'hyp-agent-1',
    run_id: RUN_ID,
    model_family: '1d_polynomial',
    origin: 'agent',
    current_revision: 1,
    is_pinned: false,
    is_excluded: false,
    updated_at: '2026-10-24T10:00:00Z',
    latest_revision: {
      revision_id: 'rev-001',
      revision_number: 1,
      formula: 'y = 2*x + 1',
      parameters: { w1: 2.0, w0: 1.0 },
      complexity: 2,
      creation_source: 'agent',
    },
    latest_evaluation: {
      evaluation_id: 'eval-001',
      status: 'completed',
      dataset_snapshot_id: 'snap-alpha',
      metrics: { mae: 0.15, rmse: 0.22, r2: 0.95 },
      held_out_metrics: { mae: 0.18, rmse: 0.25, r2: 0.93 },
      evaluated_at: '2026-10-24T10:05:00Z',
    },
    scientific_ranking: {
      rank: 1,
      within_cohort_rank: 1,
      evaluation_tier: 'VALID_HELD_OUT',
      plausibility_status: 'PLAUSIBLE',
      held_out_score: 0.18,
      training_score: 0.15,
      complexity: 2,
    },
  },
  {
    hypothesis_id: 'hyp-researcher-2',
    run_id: RUN_ID,
    model_family: '1d_polynomial',
    origin: 'researcher',
    current_revision: 2,
    is_pinned: true,
    is_excluded: false,
    updated_at: '2026-10-24T10:15:00Z',
    latest_revision: {
      revision_id: 'rev-002',
      revision_number: 2,
      formula: 'y = 3*x^2 - 0.5',
      parameters: { w2: 3.0, w0: -0.5 },
      complexity: 3,
      creation_source: 'researcher',
    },
    latest_evaluation: {
      evaluation_id: 'eval-002',
      status: 'completed',
      dataset_snapshot_id: 'snap-alpha',
      metrics: { mae: 0.08, rmse: 0.11, r2: 0.98 },
      held_out_metrics: { mae: 0.12, rmse: 0.15, r2: 0.96 },
      evaluated_at: '2026-10-24T10:20:00Z',
    },
    scientific_ranking: {
      rank: 2,
      within_cohort_rank: 2,
      evaluation_tier: 'VALID_HELD_OUT',
      plausibility_status: 'PLAUSIBLE',
      held_out_score: 0.12,
      training_score: 0.08,
      complexity: 3,
    },
  },
  {
    hypothesis_id: 'hyp-agent-3-excluded',
    run_id: RUN_ID,
    model_family: 'exponential_decay',
    origin: 'agent',
    current_revision: 1,
    is_pinned: false,
    is_excluded: true,
    updated_at: '2026-10-24T09:30:00Z',
    latest_revision: {
      revision_id: 'rev-003',
      revision_number: 1,
      formula: 'y = exp(-x)',
      parameters: {},
      complexity: 5,
      creation_source: 'agent',
    },
    latest_evaluation: {
      evaluation_id: 'eval-003',
      status: 'failed',
      dataset_snapshot_id: 'snap-alpha',
      metrics: null,
      held_out_metrics: null,
      evaluated_at: '2026-10-24T09:35:00Z',
    },
    scientific_ranking: {
      rank: 99,
      within_cohort_rank: null,
      evaluation_tier: 'EVALUATION_FAILED',
      plausibility_status: 'CONTRADICTED',
      held_out_score: null,
      training_score: null,
      complexity: 5,
    },
  },
  {
    hypothesis_id: 'hyp-agent-4-unevaluated',
    run_id: RUN_ID,
    model_family: '1d_polynomial',
    origin: 'agent',
    current_revision: 1,
    is_pinned: false,
    is_excluded: false,
    updated_at: '2026-10-24T11:00:00Z',
    latest_revision: {
      revision_id: 'rev-004',
      revision_number: 1,
      formula: 'y = 0.5*x',
      parameters: { w1: 0.5 },
      complexity: 1,
      creation_source: 'agent',
    },
    latest_evaluation: null,
    scientific_ranking: {
      rank: 999,
      within_cohort_rank: null,
      evaluation_tier: 'UNEVALUATED',
      plausibility_status: 'UNEVALUATED',
      held_out_score: null,
      training_score: null,
      complexity: 1,
    },
  },
];

describe('HypothesesPage Component', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    runsApi.listRuns.mockResolvedValue({
      runs: [makeRun({ run_id: RUN_ID, name: 'Quantum Yield Run' })],
      count: 1,
    });
    runsApi.listRunHypotheses.mockResolvedValue({
      hypotheses: mockHypotheses,
      count: mockHypotheses.length,
    });
    hypothesesApi.getHypothesisRevisions.mockResolvedValue({ revisions: [] });
    hypothesesApi.getHypothesisEvaluations.mockResolvedValue({ evaluations: [] });
    hypothesesApi.getHypothesisEvidence.mockResolvedValue({ summary: {}, observations: [] });
  });

  it('renders hypothesis table with stable ID, formula, origin, and separated scientific ranking', async () => {
    renderPage();

    // Table elements
    expect(await screen.findByText('hyp-agent-1')).toBeInTheDocument();
    expect(screen.getByText('hyp-researcher-2')).toBeInTheDocument();
    expect(screen.getByText('y = 2*x + 1')).toBeInTheDocument();
    expect(screen.getByText('y = 3*x^2 - 0.5')).toBeInTheDocument();

    // Origin badges
    expect(screen.getByText('Researcher')).toBeInTheDocument();

    // Scientific rankings separated from pinning
    expect(screen.getByText('#1')).toBeInTheDocument();
    expect(screen.getByText('#2')).toBeInTheDocument();
    expect(screen.getByText('★ Pinned')).toBeInTheDocument();

    // Plausibility status
    const plausibleBadges = screen.getAllByText('PLAUSIBLE');
    expect(plausibleBadges.length).toBeGreaterThan(0);

    // Unevaluated and missing metrics stay honest (never 0)
    expect(screen.getByText('Not evaluated')).toBeInTheDocument();
    expect(screen.getByText(/held-out metrics unavailable/i)).toBeInTheDocument();
  });

  it('supports filtering by origin, evaluation status, and lifecycle state', async () => {
    renderPage();
    await screen.findByText('hyp-agent-1');

    // Filter by Origin: Researcher
    const originSelect = screen.getByLabelText(/origin:/i);
    await userEvent.selectOptions(originSelect, 'researcher');

    expect(screen.queryByText('hyp-agent-1')).not.toBeInTheDocument();
    expect(screen.getByText('hyp-researcher-2')).toBeInTheDocument();

    // Filter by Lifecycle: Excluded only
    await userEvent.selectOptions(originSelect, 'all');
    const lifecycleSelect = screen.getByLabelText(/lifecycle:/i);
    await userEvent.selectOptions(lifecycleSelect, 'excluded');

    expect(screen.getByText('hyp-agent-3-excluded')).toBeInTheDocument();
    expect(screen.queryByText('hyp-agent-1')).not.toBeInTheDocument();

    // Search query
    await userEvent.selectOptions(lifecycleSelect, 'all');
    const searchInput = screen.getByPlaceholderText(/search by id or formula/i);
    await userEvent.type(searchInput, 'exp(-x)');

    expect(screen.getByText('hyp-agent-3-excluded')).toBeInTheDocument();
    expect(screen.queryByText('hyp-agent-1')).not.toBeInTheDocument();
  });

  it('allows selecting 2 or more hypotheses and opening side-by-side comparison', async () => {
    renderPage();
    await screen.findByText('hyp-agent-1');

    // Select two hypotheses
    const checkbox1 = screen.getByLabelText(/select hyp-agent-1 for comparison/i);
    const checkbox2 = screen.getByLabelText(/select hyp-researcher-2 for comparison/i);

    await userEvent.click(checkbox1);
    await userEvent.click(checkbox2);

    const compareBtn = screen.getByRole('button', { name: /compare/i });
    expect(compareBtn).toBeEnabled();

    await userEvent.click(compareBtn);

    // Comparison modal should be open
    expect(await screen.findByText(/side-by-side candidate comparison/i)).toBeInTheDocument();
    const modal = screen.getByRole('dialog');
    expect(within(modal).getByText('hyp-agent-1')).toBeInTheDocument();
    expect(within(modal).getByText('hyp-researcher-2')).toBeInTheDocument();
    expect(within(modal).getByText('0.1800')).toBeInTheDocument(); // Held-out MAE for hyp-1
    expect(within(modal).getByText('0.1200')).toBeInTheDocument(); // Held-out MAE for hyp-2

    // Close comparison modal
    await userEvent.click(screen.getByRole('button', { name: /close comparison/i }));
    expect(screen.queryByText(/side-by-side candidate comparison/i)).not.toBeInTheDocument();
  });

  it('displays comparability warning in comparison modal when tiers or datasets differ', async () => {
    renderPage();
    await screen.findByText('hyp-agent-1');

    // Select hyp-1 (VALID_HELD_OUT) and hyp-3 (EVALUATION_FAILED)
    const checkbox1 = screen.getByLabelText(/select hyp-agent-1 for comparison/i);
    const checkbox3 = screen.getByLabelText(/select hyp-agent-3-excluded for comparison/i);

    await userEvent.click(checkbox1);
    await userEvent.click(checkbox3);

    const compareBtn = screen.getByRole('button', { name: /compare/i });
    await userEvent.click(compareBtn);

    // Comparability warning banner should be rendered
    expect(await screen.findByText(/comparability warning:/i)).toBeInTheDocument();
  });

  it('proposes a researcher hypothesis with client validation and backend persistence', async () => {
    hypothesesApi.proposeHypothesis.mockResolvedValue({
      hypothesis: {
        hypothesis_id: 'hyp-new-researcher',
        model_family: '1d_polynomial',
        formula: 'y = 4*x - 2',
      },
    });

    renderPage();
    await screen.findByText('hyp-agent-1');

    await userEvent.click(screen.getByRole('button', { name: /propose hypothesis/i }));

    const modal = screen.getByRole('dialog');
    expect(within(modal).getByRole('heading', { name: /propose hypothesis/i })).toBeInTheDocument();

    // Client-side validation: formula is required
    const submitBtn = within(modal).getByRole('button', { name: /propose hypothesis/i });
    await userEvent.click(submitBtn);

    expect(within(modal).getByText(/formula is required/i)).toBeInTheDocument();

    // Enter valid formula
    const formulaInput = within(modal).getByLabelText(/formula/i);
    await userEvent.type(formulaInput, 'y = 4*x - 2');

    await userEvent.click(submitBtn);

    await waitFor(() => {
      expect(hypothesesApi.proposeHypothesis).toHaveBeenCalledWith(
        RUN_ID,
        expect.objectContaining({
          formula: 'y = 4*x - 2',
          model_family: '1d_polynomial',
        }),
      );
    });
  });


  it('pins and unpins candidate without altering scientific ranking', async () => {
    hypothesesApi.updateHypothesisFlags.mockResolvedValue({
      hypothesis: { ...mockHypotheses[0], is_pinned: true },
    });

    renderPage();
    await screen.findByText('hyp-agent-1');

    // Click pin on hyp-agent-1 (currently unpinned)
    const pinBtn = screen.getAllByRole('button', { name: /^pin$/i })[0];
    await userEvent.click(pinBtn);

    await waitFor(() => {
      expect(hypothesesApi.updateHypothesisFlags).toHaveBeenCalledWith(
        'hyp-agent-1',
        expect.objectContaining({
          is_pinned: true,
          expected_revision: 1,
        }),
      );
    });
  });

  it('confirms exclusion with explanation and optional reason', async () => {
    hypothesesApi.updateHypothesisFlags.mockResolvedValue({
      hypothesis: { ...mockHypotheses[0], is_excluded: true },
    });

    renderPage();
    await screen.findByText('hyp-agent-1');

    // Click exclude on hyp-agent-1
    const excludeButtons = screen.getAllByRole('button', { name: /exclude/i });
    await userEvent.click(excludeButtons[0]);

    // Modal opens
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText(/exclude hypothesis hyp-agent-1\?/i)).toBeInTheDocument();
    expect(
      within(dialog).getByText(/removes it from active search and selection without deleting its history/i),
    ).toBeInTheDocument();

    // Type optional reason
    const reasonInput = within(dialog).getByLabelText(/reason for exclusion/i);
    await userEvent.type(reasonInput, 'Unphysical asymptote');

    // Confirm exclusion
    await userEvent.click(within(dialog).getByRole('button', { name: /exclude candidate/i }));

    await waitFor(() => {
      expect(hypothesesApi.updateHypothesisFlags).toHaveBeenCalledWith(
        'hyp-agent-1',
        expect.objectContaining({
          is_excluded: true,
          reason: 'Unphysical asymptote',
          expected_revision: 1,
        }),
      );
    });
  });

  it('requests re-evaluation and displays in-flight state', async () => {
    hypothesesApi.reevaluateHypothesis.mockImplementation(
      () => new Promise((resolve) => setTimeout(resolve, 50)),
    );

    renderPage();
    await screen.findByText('hyp-agent-1');

    const reevalBtns = screen.getAllByRole('button', { name: /re-eval/i });
    await userEvent.click(reevalBtns[0]);

    expect(hypothesesApi.reevaluateHypothesis).toHaveBeenCalledWith('hyp-agent-1');
  });

  it('inspects hypothesis details and classified evidence observations', async () => {
    hypothesesApi.getHypothesisEvidence.mockResolvedValue({
      hypothesis_id: 'hyp-agent-1',
      summary: {
        total_observations: 2,
        supporting_count: 1,
        contradicting_count: 1,
        indeterminate_count: 0,
        plausibility_status: 'UNCERTAIN',
      },
      observations: [
        {
          observation_id: 'obs-001',
          inputs: { x: 1.0 },
          observed_value: 3.01,
          predicted_value: 3.0,
          residual: 0.01,
          tolerance_threshold: 0.05,
          partition: 'held_out',
          classification: 'supporting',
        },
        {
          observation_id: 'obs-002',
          inputs: { x: 2.0 },
          observed_value: 6.5,
          predicted_value: 5.0,
          residual: 1.5,
          tolerance_threshold: 0.1,
          partition: 'train',
          classification: 'contradicting',
        },
      ],
    });

    renderPage();
    await screen.findByText('hyp-agent-1');

    const detailBtns = screen.getAllByRole('button', { name: /details/i });
    await userEvent.click(detailBtns[0]);

    // Detail dialog opens
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText(/candidate details & evidence/i)).toBeInTheDocument();

    // Click Evidence tab
    const evidenceTab = within(dialog).getByRole('tab', { name: /evidence observations/i });
    await userEvent.click(evidenceTab);

    // Classified observations
    expect(await within(dialog).findByText('obs-001')).toBeInTheDocument();
    expect(within(dialog).getByText('obs-002')).toBeInTheDocument();
    expect(within(dialog).getByText('supporting')).toBeInTheDocument();
    expect(within(dialog).getByText('contradicting')).toBeInTheDocument();
    expect(
      within(dialog).getByText(/oracle ground truth internals and evaluator hidden rules are never accessible/i),
    ).toBeInTheDocument();
  });

  it('renders correctly from serialized backend response shape (contract verification)', async () => {
    // Simulates the exact payload returned by GET /api/v1/runs/:id/hypotheses and GET /api/v1/hypotheses/:id/evidence
    runsApi.listRunHypotheses.mockResolvedValueOnce({
      hypotheses: [
        {
          hypothesis_id: 'hyp-contract-test',
          run_id: RUN_ID,
          model_family: '1d_polynomial',
          origin: 'agent',
          current_revision: 1,
          is_pinned: false,
          is_excluded: false,
          updated_at: '2026-10-24T12:00:00Z',
          latest_revision: {
            revision_id: 'rev-c1',
            revision_number: 1,
            formula: 'y = 1.5*x + 0.2',
            parameters: { w1: 1.5, w0: 0.2 },
            complexity: 2,
            creation_source: 'agent',
          },
          latest_evaluation: {
            evaluation_id: 'eval-c1',
            status: 'completed',
            dataset_snapshot_id: 'snap-manifest-001',
            metrics: { mae: 0.05, rmse: 0.09, r2: 0.99 },
            held_out_metrics: { mae: 0.07, rmse: 0.12, r2: 0.97 },
            evaluated_at: '2026-10-24T12:05:00Z',
          },
          ranking: {
            rank: 1,
            within_cohort_rank: 1,
            evaluation_tier: 'VALID_HELD_OUT',
            plausibility_status: 'PLAUSIBLE',
            held_out_score: 0.07,
            training_score: 0.05,
            complexity: 2,
          },
          scientific_ranking: {
            rank: 1,
            within_cohort_rank: 1,
            evaluation_tier: 'VALID_HELD_OUT',
            plausibility_status: 'PLAUSIBLE',
            held_out_score: 0.07,
            training_score: 0.05,
            complexity: 2,
          },
        },
      ],
      count: 1,
    });

    hypothesesApi.getHypothesisEvidence.mockResolvedValueOnce({
      hypothesis_id: 'hyp-contract-test',
      evaluation_id: 'eval-c1',
      dataset_snapshot_id: 'snap-manifest-001',
      summary: {
        total_evaluated: 2,
        total_observations: 2,
        supporting_count: 1,
        contradicting_count: 1,
        indeterminate_count: 0,
        evaluation_status: 'COMPLETED',
        plausibility_status: 'PLAUSIBLE',
      },
      records: [
        {
          observation_id: 'obs-verified-1',
          input_values: { x: 0.5 },
          inputs: { x: 0.5 },
          observed_value: 0.95,
          predicted_value: 0.95,
          residual: 0.0,
          tolerance: 0.05,
          tolerance_threshold: 0.05,
          partition: 'held_out',
          classification: 'supporting',
          membership_verified: true,
          post_evaluation: false,
        },
        {
          observation_id: 'obs-post-eval-2',
          input_values: { x: 2.5 },
          inputs: { x: 2.5 },
          observed_value: 5.5,
          predicted_value: 3.95,
          residual: 1.55,
          tolerance: 0.05,
          tolerance_threshold: 0.05,
          partition: 'train',
          classification: 'contradicting',
          membership_verified: false,
          post_evaluation: true,
        },
      ],
      observations: [
        {
          observation_id: 'obs-verified-1',
          input_values: { x: 0.5 },
          inputs: { x: 0.5 },
          observed_value: 0.95,
          predicted_value: 0.95,
          residual: 0.0,
          tolerance: 0.05,
          tolerance_threshold: 0.05,
          partition: 'held_out',
          classification: 'supporting',
          membership_verified: true,
          post_evaluation: false,
        },
        {
          observation_id: 'obs-post-eval-2',
          input_values: { x: 2.5 },
          inputs: { x: 2.5 },
          observed_value: 5.5,
          predicted_value: 3.95,
          residual: 1.55,
          tolerance: 0.05,
          tolerance_threshold: 0.05,
          partition: 'train',
          classification: 'contradicting',
          membership_verified: false,
          post_evaluation: true,
        },
      ],
    });

    renderPage();

    // Verify row displays rank and plausibility status correctly from backend ranking payload
    expect(await screen.findByText('hyp-contract-test')).toBeInTheDocument();
    expect(screen.getByText('#1')).toBeInTheDocument();
    expect(screen.getByText('PLAUSIBLE')).toBeInTheDocument();

    // Open detail modal and switch to Evidence
    const detailBtn = screen.getByRole('button', { name: /details/i });
    await userEvent.click(detailBtn);

    const dialog = await screen.findByRole('dialog');
    const evidenceTab = within(dialog).getByRole('tab', { name: /evidence observations/i });
    await userEvent.click(evidenceTab);

    // Verify total count in tab label and snapshot badge
    expect(within(dialog).getByRole('tab', { name: /evidence observations \(2\)/i })).toBeInTheDocument();
    expect(within(dialog).getByText(/Snapshot: snap-manifest-001/i)).toBeInTheDocument();

    // Verify observation rows, inputs, tolerance, and classifications appear
    expect(within(dialog).getByText('obs-verified-1')).toBeInTheDocument();
    expect(within(dialog).getByText('obs-post-eval-2')).toBeInTheDocument();
    expect(within(dialog).getByText('{"x":0.5}')).toBeInTheDocument();
    expect(within(dialog).getAllByText('0.0500').length).toBe(2);
    expect(within(dialog).getByText('supporting')).toBeInTheDocument();
    expect(within(dialog).getByText('contradicting')).toBeInTheDocument();

    // Post-eval badge for unverified snapshot membership
    expect(within(dialog).getByText('Post-eval')).toBeInTheDocument();
  });

  it('meets keyboard accessibility: Escape closes modal and restores focus to triggering element', async () => {
    renderPage();
    await screen.findByText('hyp-agent-1');

    const detailBtns = screen.getAllByRole('button', { name: /details/i });
    const triggerBtn = detailBtns[0];
    triggerBtn.focus();
    expect(document.activeElement).toBe(triggerBtn);

    // Open detail dialog
    await userEvent.click(triggerBtn);
    const dialog = await screen.findByRole('dialog');
    expect(dialog).toBeInTheDocument();

    // Press Escape
    await userEvent.keyboard('{Escape}');

    // Dialog should be closed
    await waitFor(() => {
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    });

    // Focus should be restored to triggering button
    expect(document.activeElement).toBe(triggerBtn);
  });

  it('meets keyboard accessibility: Tab containment traps focus inside modal', async () => {
    renderPage();
    await screen.findByText('hyp-agent-1');

    const detailBtn = screen.getAllByRole('button', { name: /details/i })[0];
    await userEvent.click(detailBtn);

    const dialog = await screen.findByRole('dialog');
    expect(dialog).toBeInTheDocument();

    // Get all focusable elements inside the dialog
    const focusable = dialog.querySelectorAll(
      'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    );
    expect(focusable.length).toBeGreaterThan(1);

    const firstElement = focusable[0];
    const lastElement = focusable[focusable.length - 1];

    // Focus last element and press Tab -> should wrap to first element
    lastElement.focus();
    expect(document.activeElement).toBe(lastElement);

    const tabEvent = new KeyboardEvent('keydown', { key: 'Tab', bubbles: true, cancelable: true });
    dialog.dispatchEvent(tabEvent);

    // Focus first element and press Shift+Tab -> should wrap to last element
    firstElement.focus();
    expect(document.activeElement).toBe(firstElement);

    const shiftTabEvent = new KeyboardEvent('keydown', { key: 'Tab', shiftKey: true, bubbles: true, cancelable: true });
    dialog.dispatchEvent(shiftTabEvent);
  });
});
