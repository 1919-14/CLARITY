import { Link } from 'react-router-dom';

import { OrnamentDivider, PageHeader } from '../components/layout/PageHeader.jsx';

export function NotFoundPage() {
  return (
    <div className="page-inner page-inner--narrow">
      <PageHeader
        volume="Volume IX · Colophon"
        title="This leaf is missing"
        description="The requested page does not exist in this archive. It may have been moved, or the address may be mistyped."
      />
      <OrnamentDivider />
      <div className="state-block state-block--empty">
        <p className="empty-mark" aria-hidden="true">❧</p>
        <h2 className="state-block__title">Nothing is recorded here</h2>
        <p className="state-block__message">
          Return to the research dashboard to inspect persisted runs and the scientific
          workflows that exist in this build.
        </p>
        <div className="state-block__action">
          <Link className="button button--primary" to="/">Return to the dashboard</Link>
        </div>
      </div>
    </div>
  );
}
