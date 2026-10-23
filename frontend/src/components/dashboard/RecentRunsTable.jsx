import { Link } from 'react-router-dom';

import {
  executionModeLabel,
  formatDateTime,
  formatInteger,
  formatSeconds,
} from '../../utils/formatting.js';
import { StatusBadge } from '../status/StatusBadge.jsx';

export function RecentRunsTable({ runs }) {
  return (
    <div className="table-scroll">
      <table className="data-table">
        <caption className="visually-hidden">Recent discovery runs, newest first</caption>
        <thead>
          <tr>
            <th scope="col">Run</th>
            <th scope="col">Name</th>
            <th scope="col">Created</th>
            <th scope="col">Status</th>
            <th scope="col">Mode</th>
            <th scope="col">Limits</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((run) => (
            <tr key={run.run_id}>
              <td data-label="Run">
                <Link className="id-link" to={`/runs/${encodeURIComponent(run.run_id)}`}>
                  {run.run_id}
                </Link>
              </td>
              <td data-label="Name">{run.name}</td>
              <td data-label="Created">{formatDateTime(run.created_at)}</td>
              <td data-label="Status">
                <StatusBadge status={run.status} />
              </td>
              <td data-label="Mode">{executionModeLabel(run.execution_mode)}</td>
              <td data-label="Limits">
                {formatInteger(run.max_queries)} queries · {formatInteger(run.max_evaluations)} evaluations ·{' '}
                {formatSeconds(run.max_runtime_seconds)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
