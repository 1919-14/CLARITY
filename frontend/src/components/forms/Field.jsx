import { cloneElement, isValidElement } from 'react';

/**
 * Associates a label, hint and error message with one form control, so screen
 * readers announce validation feedback and the error is discoverable by id.
 */
export function Field({ id, label, hint = null, error = null, required = false, children }) {
  const hintId = hint ? `${id}-hint` : undefined;
  const errorId = error ? `${id}-error` : undefined;
  const describedBy = [hintId, errorId].filter(Boolean).join(' ') || undefined;

  const control = isValidElement(children)
    ? cloneElement(children, {
        id,
        'aria-describedby': describedBy,
        'aria-invalid': error ? true : undefined,
      })
    : children;

  return (
    <div className={`field${error ? ' field--invalid' : ''}`}>
      <label className="field__label" htmlFor={id}>
        {label}
        {required ? (
          <>
            <span aria-hidden="true"> *</span>
            <span className="visually-hidden"> (required)</span>
          </>
        ) : null}
      </label>
      {hint ? (
        <p className="field__hint" id={hintId}>
          {hint}
        </p>
      ) : null}
      {control}
      {error ? (
        <p className="field__error" id={errorId}>
          <span aria-hidden="true">↳ </span>
          {error}
        </p>
      ) : null}
    </div>
  );
}
