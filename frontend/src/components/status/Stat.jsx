import { UNAVAILABLE } from '../../utils/formatting.js';

export function Stat({ label, value, hint = null, mono = false, className = '' }) {
  const isUnavailable = value === UNAVAILABLE || value === null || value === undefined;
  return (
    <div className={`stat${className ? ` ${className}` : ''}`}>
      <dt className="stat__label">{label}</dt>
      <dd className={`stat__value${isUnavailable ? ' stat__value--unavailable' : ''}${mono ? ' stat__value--mono' : ''}`}>
        {isUnavailable ? UNAVAILABLE : value}
      </dd>
      {hint ? <dd className="stat__hint">{hint}</dd> : null}
    </div>
  );
}
