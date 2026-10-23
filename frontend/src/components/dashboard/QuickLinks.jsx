import { Link } from 'react-router-dom';

import { ChartIcon, ChevronRightIcon, FlaskIcon, ScrollIcon } from '../icons.jsx';

const WORKFLOWS = [
  {
    to: '/hypotheses',
    title: 'Hypotheses',
    icon: FlaskIcon,
    description: 'Inspect the candidate explanations recorded for a run, their revisions and latest evaluation.',
  },
  {
    to: '/reports',
    title: 'Scientific reports',
    icon: ScrollIcon,
    description: 'Read or generate the ranked summary of a run, including its recorded limitations.',
  },
  {
    to: '/benchmarks',
    title: 'Benchmarks',
    icon: ChartIcon,
    description: 'Review registered scenarios and the benchmark results stored for a run.',
  },
];

export function QuickLinks() {
  return (
    <section className="workflow-section" aria-labelledby="workflow-heading">
      <div className="section-heading-row">
        <p className="volume-label">Volume IV</p>
        <h2 id="workflow-heading" className="section-heading">Scientific workflows</h2>
      </div>
      <div className="workflow-grid">
        {WORKFLOWS.map(({ to, title, description, icon: Icon }) => (
          <Link key={to} to={to} className="workflow-card">
            <span className="workflow-card__icon" aria-hidden="true">
              <Icon size={22} />
            </span>
            <span className="workflow-card__body">
              <span className="workflow-card__title">{title}</span>
              <span className="workflow-card__description">{description}</span>
            </span>
            <ChevronRightIcon size={18} />
          </Link>
        ))}
      </div>
    </section>
  );
}
