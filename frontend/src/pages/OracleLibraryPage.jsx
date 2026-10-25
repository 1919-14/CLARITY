import { useCallback, useState } from 'react';
import { Link } from 'react-router-dom';

import {
  ARTIFACT_KINDS,
  createOracle,
  deleteOracle,
  kindForFilename,
  listOracles,
} from '../api/oracles.js';
import { EmptyState } from '../components/feedback/EmptyState.jsx';
import { ErrorState } from '../components/feedback/ErrorState.jsx';
import { LoadingState } from '../components/feedback/LoadingState.jsx';
import { Notice } from '../components/feedback/Notice.jsx';
import { Field } from '../components/forms/Field.jsx';
import { ConfirmDialog } from '../components/forms/ConfirmDialog.jsx';
import { RefreshIcon } from '../components/icons.jsx';
import { OrnamentDivider, PageHeader } from '../components/layout/PageHeader.jsx';
import { useResource } from '../hooks/useResource.js';
import { formatDateTime, formatDecimal, formatInteger, UNAVAILABLE } from '../utils/formatting.js';

const EMPTY_FORM = Object.freeze({
  file: null,
  name: '',
  entrypoint: '',
  inputName: 'x',
  outputName: 'y',
  minValue: '-10',
  maxValue: '10',
});

function kindLabel(kindValue) {
  return ARTIFACT_KINDS.find((kind) => kind.value === kindValue)?.label || kindValue || UNAVAILABLE;
}

function validateUpload(values) {
  const errors = {};
  const kind = kindForFilename(values.file?.name);

  if (!values.file) {
    errors.file = 'Choose an artifact to upload.';
  } else if (!kind) {
    errors.file = 'Supported artifacts are .py, .joblib, .pkl, .pickle and .onnx files.';
  }

  if (kind === 'python_function' && !values.entrypoint.trim()) {
    errors.entrypoint = 'Name the function CLARITY should call, e.g. simulate.';
  }

  const min = Number(values.minValue);
  const max = Number(values.maxValue);
  if (String(values.minValue).trim() === '' || !Number.isFinite(min)) {
    errors.minValue = 'Enter a finite lower bound.';
  }
  if (String(values.maxValue).trim() === '' || !Number.isFinite(max)) {
    errors.maxValue = 'Enter a finite upper bound.';
  }
  if (!errors.minValue && !errors.maxValue && min >= max) {
    errors.maxValue = 'The upper bound must be larger than the lower bound.';
  }

  return { errors, kind };
}

function probeText(probe) {
  if (!probe || !Array.isArray(probe.samples) || probe.samples.length === 0) {
    return UNAVAILABLE;
  }
  return probe.samples
    .map((sample) =>
      sample.error ? `f(${formatDecimal(sample.x, 3)}) failed` : `f(${formatDecimal(sample.x, 3)}) = ${formatDecimal(sample.y, 3)}`,
    )
    .join(' · ');
}

function UploadForm({ onUploaded }) {
  const [values, setValues] = useState({ ...EMPTY_FORM });
  const [errors, setErrors] = useState({});
  const [submitting, setSubmitting] = useState(false);
  const [requestError, setRequestError] = useState(null);

  const update = (field) => (event) => {
    const value = field === 'file' ? event.target.files?.[0] || null : event.target.value;
    setValues((previous) => {
      const next = { ...previous, [field]: value };
      if (field === 'file' && value && !previous.name.trim()) {
        next.name = value.name.replace(/\.[^.]+$/, '');
      }
      return next;
    });
    if (errors[field]) {
      setErrors((previous) => {
        const next = { ...previous };
        delete next[field];
        return next;
      });
    }
  };

  const kind = kindForFilename(values.file?.name);

  const handleSubmit = async (event) => {
    event.preventDefault();
    if (submitting) return;
    const validation = validateUpload(values);
    setErrors(validation.errors);
    setRequestError(null);
    if (Object.keys(validation.errors).length > 0) return;

    setSubmitting(true);
    try {
      const response = await createOracle(values.file, {
        name: values.name.trim(),
        kind: validation.kind,
        entrypoint: values.entrypoint.trim(),
        inputName: values.inputName.trim() || 'x',
        outputName: values.outputName.trim() || 'y',
        minValue: values.minValue,
        maxValue: values.maxValue,
      });
      onUploaded(response.oracle);
      setValues({ ...EMPTY_FORM });
    } catch (error) {
      if (!error?.isAborted) setRequestError(error);
    } finally {
      setSubmitting(false);
    }
  };

  const activeKind = ARTIFACT_KINDS.find((entry) => entry.value === kind);

  return (
    <form className="form-card form-card--ornate" onSubmit={handleSubmit} noValidate>
      <div className="panel__header">
        <div>
          <p className="volume-label">Register an artifact</p>
          <h2 className="panel__title">Upload a black box</h2>
          <p className="panel__subtitle">
            CLARITY probes only the artifact&apos;s inputs and outputs. It never inspects the
            internals and never claims a ground-truth formula for it.
          </p>
        </div>
      </div>

      <Notice tone="warning" title="Uploaded artifacts run inside the backend">
        Python modules are imported and pickled models are unpickled in the Flask process, so
        upload only code you trust. CLARITY contains failures as recorded observations, but it
        does not sandbox the artifact.
      </Notice>

      {requestError ? (
        <ErrorState compact title="The artifact was not registered" error={requestError} />
      ) : null}

      <Field
        id="oracle-file"
        label="Artifact file"
        required
        hint={
          activeKind?.hint ||
          'Python function (.py), joblib/pickle model (.joblib, .pkl, .pickle) or ONNX model (.onnx).'
        }
        error={errors.file}
      >
        <input
          id="oracle-file"
          className="input input--file"
          type="file"
          accept=".py,.joblib,.pkl,.pickle,.onnx"
          onChange={update('file')}
        />
        {values.file ? (
          <div className="file-selection-badge">
            <span className="file-selection-badge__name">{values.file.name}</span>
            <span className="file-selection-badge__meta">
              ({formatInteger(Math.round(values.file.size / 1024))} KB) · {kindLabel(kind)}
            </span>
          </div>
        ) : null}
      </Field>

      <div className="form-grid">
        <Field id="oracle-name" label="Library name" hint="How this artifact is listed.">
          <input
            id="oracle-name"
            className="input"
            type="text"
            value={values.name}
            onChange={update('name')}
            placeholder="e.g. Reactor pressure simulator"
            autoComplete="off"
          />
        </Field>

        <Field
          id="oracle-kind"
          label="Detected kind"
          hint="Determined by the file extension."
        >
          <input
            id="oracle-kind"
            className="input"
            type="text"
            value={kind ? kindLabel(kind) : UNAVAILABLE}
            readOnly
          />
        </Field>

        {kind === 'python_function' ? (
          <Field
            id="oracle-entrypoint"
            label="Function to call"
            required
            hint="The function receives one number and must return one number."
            error={errors.entrypoint}
          >
            <input
              id="oracle-entrypoint"
              className="input"
              type="text"
              value={values.entrypoint}
              onChange={update('entrypoint')}
              placeholder="simulate"
              autoComplete="off"
            />
          </Field>
        ) : null}

        <Field id="oracle-min" label="Lower bound" required error={errors.minValue} hint="Finite number.">
          <input
            id="oracle-min"
            className="input input--numeric"
            type="text"
            inputMode="decimal"
            value={values.minValue}
            onChange={update('minValue')}
          />
        </Field>

        <Field id="oracle-max" label="Upper bound" required error={errors.maxValue} hint="Finite number.">
          <input
            id="oracle-max"
            className="input input--numeric"
            type="text"
            inputMode="decimal"
            value={values.maxValue}
            onChange={update('maxValue')}
          />
        </Field>

        <Field id="oracle-input" label="Input name" hint="Label used in observations.">
          <input
            id="oracle-input"
            className="input"
            type="text"
            value={values.inputName}
            onChange={update('inputName')}
          />
        </Field>

        <Field id="oracle-output" label="Output name" hint="Label used in observations.">
          <input
            id="oracle-output"
            className="input"
            type="text"
            value={values.outputName}
            onChange={update('outputName')}
          />
        </Field>
      </div>

      <div className="form-actions">
        <button
          type="submit"
          className="button button--primary"
          disabled={submitting}
          aria-busy={submitting || undefined}
        >
          <span>{submitting ? 'Probing artifact…' : 'Register artifact'}</span>
        </button>
        <p className="form-actions__note" role="status">
          Registration probes three inputs across the declared range. If the artifact cannot answer,
          nothing is registered.
        </p>
      </div>
    </form>
  );
}

export function OracleLibraryPage() {
  const loader = useCallback(({ signal }) => listOracles({ signal }), []);
  const { data, isLoading, isRefreshing, error, reload } = useResource(loader);
  const oracles = data?.oracles ?? [];
  const [lastRegistered, setLastRegistered] = useState(null);
  const [deleteTarget, setDeleteTarget] = useState(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState(null);

  const handleUploaded = (oracle) => {
    setLastRegistered(oracle);
    reload();
  };

  const handleDelete = async () => {
    if (!deleteTarget || deleting) return;
    setDeleting(true);
    setDeleteError(null);
    try {
      await deleteOracle(deleteTarget.oracle_id);
      setDeleteTarget(null);
      reload();
    } catch (deleteFailure) {
      if (!deleteFailure?.isAborted) setDeleteError(deleteFailure);
    } finally {
      setDeleting(false);
    }
  };

  return (
    <div className="page-inner">
      <PageHeader
        volume="Volume IV · Black boxes"
        title="Oracle library"
        description="Upload your own simulator or trained model and CLARITY will probe it, then distil the formula its responses support. Artifacts are reusable across runs."
        actions={
          <button
            type="button"
            className="button button--ghost button--small"
            onClick={reload}
            disabled={isRefreshing}
            aria-busy={isRefreshing || undefined}
          >
            <RefreshIcon size={16} />
            <span>{isRefreshing ? 'Refreshing…' : 'Refresh library'}</span>
          </button>
        }
      />
      <OrnamentDivider />

      {lastRegistered ? (
        <Notice tone="success" title="Artifact registered">
          <p>
            <strong>{lastRegistered.name}</strong> ({lastRegistered.kind_label}) answered the
            registration probe: {probeText(lastRegistered.probe)}.
          </p>
          <p>
            <Link className="text-link" to={`/runs/new?oracle=${encodeURIComponent(lastRegistered.oracle_id)}`}>
              Create a discovery run against this artifact
            </Link>
          </p>
        </Notice>
      ) : null}

      <UploadForm onUploaded={handleUploaded} />

      <section aria-labelledby="oracle-list-heading">
        <div className="section-heading-row">
          <p className="volume-label">Registered artifacts</p>
          <h2 id="oracle-list-heading" className="section-heading">
            Library
          </h2>
          <p className="panel-note">
            {oracles.length === 0
              ? 'No artifacts have been registered yet.'
              : `${formatInteger(oracles.length)} artifact${oracles.length === 1 ? '' : 's'} registered.`}
          </p>
        </div>

        {isLoading ? <LoadingState label="Listing registered artifacts…" /> : null}
        {!isLoading && error ? (
          <ErrorState title="The oracle library could not be loaded" error={error} onRetry={reload} />
        ) : null}
        {deleteError ? (
          <ErrorState compact title="The artifact could not be deleted" error={deleteError} />
        ) : null}

        {!isLoading && !error && oracles.length === 0 ? (
          <EmptyState
            title="No black boxes yet"
            message="Upload a simulation script or a trained model above. You can then create a run against it, and CLARITY will query it like any other black box."
          />
        ) : null}

        {!isLoading && !error && oracles.length > 0 ? (
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Artifact</th>
                  <th>Kind</th>
                  <th>Declared contract</th>
                  <th>Registration probe</th>
                  <th>Runs</th>
                  <th>Registered</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {oracles.map((oracle) => (
                  <tr key={oracle.oracle_id}>
                    <td data-label="Artifact">
                      <strong>{oracle.name}</strong>
                      <span className="block-note muted">
                        <code className="id-text">{oracle.oracle_id}</code>
                      </span>
                      <span className="block-note muted">{oracle.original_filename}</span>
                    </td>
                    <td data-label="Kind">{oracle.kind_label || kindLabel(oracle.kind)}</td>
                    <td data-label="Declared contract">
                      <span>
                        {oracle.entrypoint ? `${oracle.entrypoint}(${oracle.input_name})` : `${oracle.input_name} → ${oracle.output_name}`}
                      </span>
                      <span className="block-note muted">
                        {formatDecimal(oracle.min_value, 3)} … {formatDecimal(oracle.max_value, 3)}
                      </span>
                    </td>
                    <td data-label="Registration probe">
                      <span className="muted">{probeText(oracle.probe)}</span>
                    </td>
                    <td data-label="Runs">{formatInteger(oracle.run_count)}</td>
                    <td data-label="Registered">{formatDateTime(oracle.created_at)}</td>
                    <td data-label="Actions">
                      <div style={{ display: 'flex', gap: 'var(--space-2)', flexWrap: 'wrap' }}>
                        <Link
                          className="button button--secondary button--small"
                          to={`/runs/new?oracle=${encodeURIComponent(oracle.oracle_id)}`}
                        >
                          Create run
                        </Link>
                        <button
                          type="button"
                          className="button button--ghost button--small text-danger"
                          onClick={() => {
                            setDeleteError(null);
                            setDeleteTarget(oracle);
                          }}
                        >
                          Delete
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
      </section>

      <ConfirmDialog
        open={Boolean(deleteTarget)}
        title="Delete this artifact?"
        description={
          deleteTarget
            ? `${deleteTarget.name} will be removed from the library along with its stored file. Runs already bound to it keep their records, but the artifact itself cannot be deleted while a run references it.`
            : ''
        }
        confirmLabel="Delete artifact"
        tone="danger"
        busy={deleting}
        onConfirm={handleDelete}
        onCancel={() => setDeleteTarget(null)}
      />
    </div>
  );
}
