// fetch helper. Backend errors are {"error":"code","message":"deutscher Text"}; they surface as a toast and an ApiError.
import { toast } from './toast.js';

export class ApiError extends Error {
  constructor(status, code, message, body) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.body = body;
  }
}

async function request(method, path, body, options = {}) {
  const init = { method, headers: { Accept: 'application/json' } };
  if (body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  if (options.signal) init.signal = options.signal;

  let response;
  try {
    response = await fetch(path, init);
  } catch (cause) {
    if (cause?.name === 'AbortError') throw cause;
    const error = new ApiError(0, 'network', 'Das MES ist nicht erreichbar.', null);
    surface(error, options);
    throw error;
  }

  let data = null;
  const text = await response.text();
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = null;
    }
  }
  if (response.ok) return data;

  const message = data?.message ?? `Der Server antwortet mit Fehler ${response.status}.`;
  const error = new ApiError(response.status, data?.error ?? 'http_error', message, data);
  surface(error, options);
  throw error;
}

// `quiet: true` silences all toasts, `quiet: [409, 422]` only those statuses, for callers that show the problem in place.
function surface(error, { quiet }) {
  if (quiet === true) return;
  if (Array.isArray(quiet) && quiet.includes(error.status)) return;
  toast(error.message, { kind: 'error' });
}

export const api = {
  get: (path, options) => request('GET', path, undefined, options),
  post: (path, body = {}, options) => request('POST', path, body, options),
};
