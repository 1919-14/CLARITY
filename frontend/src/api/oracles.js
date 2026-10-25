/**
 * API client for the uploaded black-box oracle library.
 *
 * Artifacts are uploaded as multipart/form-data; everything else is JSON. The
 * backend never returns stored file paths or artifact bytes.
 */

import { apiDelete, apiGet, apiPost } from './client.js';

const enc = (value) => encodeURIComponent(String(value));

/** GET /oracles */
export function listOracles({ signal } = {}) {
  return apiGet('/oracles', { signal });
}

/** GET /oracles/<oracle_id> */
export function getOracle(oracleId, { signal } = {}) {
  return apiGet(`/oracles/${enc(oracleId)}`, { signal });
}

/**
 * POST /oracles (multipart/form-data)
 *
 * @param {File} file the artifact (.py, .joblib/.pkl/.pickle, .onnx)
 * @param {object} fields declared contract: name, kind, entrypoint, inputName,
 *   outputName, minValue, maxValue
 */
export function createOracle(file, fields = {}, { signal } = {}) {
  const form = new FormData();
  form.append('file', file);
  const mapping = {
    name: 'name',
    kind: 'kind',
    entrypoint: 'entrypoint',
    inputName: 'input_name',
    outputName: 'output_name',
    minValue: 'min_value',
    maxValue: 'max_value',
  };
  Object.entries(mapping).forEach(([key, formKey]) => {
    const value = fields[key];
    if (value !== undefined && value !== null && value !== '') {
      form.append(formKey, String(value));
    }
  });
  return apiPost('/oracles', form, { signal });
}

/** DELETE /oracles/<oracle_id> */
export function deleteOracle(oracleId, { signal } = {}) {
  return apiDelete(`/oracles/${enc(oracleId)}`, { signal });
}

/** Artifact kind inferred from a filename extension, or null when unsupported. */
export const ARTIFACT_KINDS = [
  {
    value: 'python_function',
    label: 'Python function',
    extensions: ['.py'],
    hint: 'A .py file exposing a function such as def simulate(x): return 2*x + 1.',
  },
  {
    value: 'joblib_model',
    label: 'Joblib / pickle model',
    extensions: ['.joblib', '.pkl', '.pickle'],
    hint: 'An object with predict([[x]]) — a scikit-learn estimator, for example.',
  },
  {
    value: 'onnx_model',
    label: 'ONNX model',
    extensions: ['.onnx'],
    hint: 'A graph taking one float feature (shape [1, 1]) and returning one float.',
  },
];

export function kindForFilename(filename) {
  const lower = String(filename || '').toLowerCase();
  const match = ARTIFACT_KINDS.find((kind) =>
    kind.extensions.some((extension) => lower.endsWith(extension)),
  );
  return match?.value || '';
}
