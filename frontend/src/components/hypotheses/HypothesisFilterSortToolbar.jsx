import { CompareIcon, PlusIcon } from '../icons.jsx';

export function HypothesisFilterSortToolbar({
  filterOrigin,
  setFilterOrigin,
  filterStatus,
  setFilterStatus,
  filterLifecycle,
  setFilterLifecycle,
  searchQuery,
  setSearchQuery,
  sortBy,
  setSortBy,
  sortOrder,
  setSortOrder,
  selectedCount,
  onCompare,
  onPropose,
}) {
  return (
    <div className="toolbar hypothesis-toolbar" role="region" aria-label="Hypothesis filters and controls">
      <div className="toolbar__group toolbar__group--search">
        <label htmlFor="hyp-search" className="visually-hidden">Search hypotheses</label>
        <input
          id="hyp-search"
          type="search"
          className="input input--small"
          placeholder="Search by ID or formula…"
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
        />
      </div>

      <div className="toolbar__group">
        <label htmlFor="filter-origin" className="field__inline-label">Origin:</label>
        <select
          id="filter-origin"
          className="select select--small"
          value={filterOrigin}
          onChange={(e) => setFilterOrigin(e.target.value)}
        >
          <option value="all">All origins</option>
          <option value="agent">Agent generated</option>
          <option value="researcher">Researcher proposed</option>
        </select>
      </div>

      <div className="toolbar__group">
        <label htmlFor="filter-status" className="field__inline-label">Evaluation:</label>
        <select
          id="filter-status"
          className="select select--small"
          value={filterStatus}
          onChange={(e) => setFilterStatus(e.target.value)}
        >
          <option value="all">All statuses</option>
          <option value="completed">Evaluated</option>
          <option value="pending">Pending</option>
          <option value="failed">Evaluation failed</option>
          <option value="unevaluated">Unevaluated</option>
        </select>
      </div>

      <div className="toolbar__group">
        <label htmlFor="filter-lifecycle" className="field__inline-label">Lifecycle:</label>
        <select
          id="filter-lifecycle"
          className="select select--small"
          value={filterLifecycle}
          onChange={(e) => setFilterLifecycle(e.target.value)}
        >
          <option value="all">All lifecycles</option>
          <option value="active">Active only</option>
          <option value="pinned">Pinned only</option>
          <option value="excluded">Excluded only</option>
        </select>
      </div>

      <div className="toolbar__group">
        <label htmlFor="sort-by" className="field__inline-label">Sort by:</label>
        <select
          id="sort-by"
          className="select select--small"
          value={sortBy}
          onChange={(e) => setSortBy(e.target.value)}
        >
          <option value="rank">Scientific rank</option>
          <option value="held_out_mae">Held-out MAE (lower is better)</option>
          <option value="held_out_rmse">Held-out RMSE</option>
          <option value="training_mae">Training MAE</option>
          <option value="complexity">Complexity</option>
          <option value="revision">Revision</option>
          <option value="updated_at">Recently updated</option>
        </select>

        <button
          type="button"
          className="button button--secondary button--small"
          onClick={() => setSortOrder(sortOrder === 'asc' ? 'desc' : 'asc')}
          title={`Order: ${sortOrder === 'asc' ? 'Ascending' : 'Descending'}`}
          aria-label={`Toggle sort order, current: ${sortOrder === 'asc' ? 'Ascending' : 'Descending'}`}
        >
          {sortOrder === 'asc' ? '↑ Asc' : '↓ Desc'}
        </button>
      </div>

      <div className="toolbar__group toolbar__group--actions">
        <button
          type="button"
          className="button button--secondary button--small"
          onClick={onCompare}
          disabled={selectedCount < 2}
          title={selectedCount < 2 ? 'Select at least 2 hypotheses to compare' : 'Compare selected hypotheses'}
          aria-label={`Compare ${selectedCount} selected hypotheses`}
        >
          <CompareIcon size={16} />
          <span>Compare ({selectedCount})</span>
        </button>

        <button
          type="button"
          className="button button--primary button--small"
          onClick={onPropose}
        >
          <PlusIcon size={16} />
          <span>Propose hypothesis</span>
        </button>
      </div>
    </div>
  );
}
