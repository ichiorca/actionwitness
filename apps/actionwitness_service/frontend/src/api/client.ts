/**
 * The one place this app talks to the harness API (§15, constitution §5).
 *
 * Every relative `/api/v1` request goes through `request()`, which is what makes
 * three rules true everywhere rather than wherever someone remembered them:
 *
 * - **`response.ok` is checked.** `fetch` rejects only on network failure; a 409
 *   is a perfectly resolved promise, and code that forgot to look would read a
 *   refusal as a success.
 * - **The body is treated as untrusted.** It arrives as `unknown` and is
 *   narrowed by a validator the caller supplies. The server is ours, but the
 *   response is still data crossing a boundary, and a UI that trusted its shape
 *   would crash on the first schema change instead of reporting one.
 * - **Every request takes an `AbortSignal`.** A component that unmounts mid-poll
 *   must be able to stop caring, or a stale response overwrites fresh state.
 *
 * Errors surface as `ApiError`, carrying §15.8's envelope when the server sent
 * one. Callers get a stable shape whether the failure was a refusal, a network
 * drop, or a body that did not parse — three cases that otherwise each need
 * their own handling at every call site.
 */

export const API_PREFIX = "/api/v1";

/** §15.8's error envelope, as far as a client needs it. */
export interface ApiErrorEnvelope {
  readonly code: string;
  readonly message: string;
  readonly retryable: boolean;
  readonly details: ReadonlyArray<{ readonly path: string; readonly message: string }>;
}

export class ApiError extends Error {
  readonly status: number;
  readonly envelope: ApiErrorEnvelope | null;

  constructor(message: string, status: number, envelope: ApiErrorEnvelope | null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.envelope = envelope;
  }

  /** The stable code a caller branches on, or `""` when the server sent none. */
  get code(): string {
    return this.envelope?.code ?? "";
  }
}

export interface RequestOptions<T> {
  readonly method?: "GET" | "POST" | "PUT" | "DELETE";
  readonly body?: unknown;
  /**
   * A body sent exactly as given, for the routes that read raw bytes.
   *
   * The evaluator import reads `await request.body()` so FR-117's size cap can
   * precede the JSON parser. Handing it a re-serialized object would still
   * work, but it would mean the bytes the operator chose and the bytes the
   * server measured were different — and the one place that matters is the
   * cap, which exists to bound what an *uploaded file* can do. Mutually
   * exclusive with `body`; passing both is a caller error.
   */
  readonly rawBody?: string;
  // `| undefined` is deliberate under exactOptionalPropertyTypes: callers
  // forward an invocation signal that the pinned build never supplies
  // (ADR-0002), so an explicit undefined must be assignable.
  readonly signal?: AbortSignal | undefined;
  /**
   * Narrows the response body. Required rather than optional: an unvalidated
   * `as T` is the exact move the constitution forbids at a boundary, and making
   * it the default would leave every caller one keystroke from doing it.
   */
  readonly parse: (value: unknown) => T;
}

export async function request<T>(path: string, options: RequestOptions<T>): Promise<T> {
  const { method = "GET", body, rawBody, signal, parse } = options;

  const sent =
    rawBody !== undefined
      ? { headers: { "Content-Type": "application/json" }, body: rawBody }
      : body === undefined
        ? {}
        : { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };

  let response: Response;
  try {
    response = await fetch(`${API_PREFIX}${path}`, {
      method,
      // The workspace cookie is the authorization boundary (§20.1), so it has
      // to travel — and only to this origin.
      credentials: "same-origin",
      ...sent,
      ...(signal === undefined ? {} : { signal }),
    });
  } catch (error: unknown) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw error; // Cancellation is not a failure; let the caller ignore it.
    }
    throw new ApiError("The harness could not be reached.", 0, null);
  }

  const payload = await readBody(response);

  if (!response.ok) {
    throw new ApiError(
      envelopeOf(payload)?.message ?? `The request failed (${String(response.status)}).`,
      response.status,
      envelopeOf(payload),
    );
  }

  try {
    return parse(payload);
  } catch (error: unknown) {
    // A body that does not match its contract is a server-side problem, and
    // saying so is more useful than a TypeError from three components deeper.
    throw new ApiError(
      error instanceof Error ? error.message : "The response was not in the expected shape.",
      response.status,
      null,
    );
  }
}

/**
 * The parsed body, or `null`.
 *
 * A 204 and an empty 200 both have no body, and `response.json()` throws on
 * both — which would turn a successful delete into an error.
 */
async function readBody(response: Response): Promise<unknown> {
  const text = await response.text();
  if (text === "") {
    return null;
  }
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return null;
  }
}

function envelopeOf(payload: unknown): ApiErrorEnvelope | null {
  if (!isRecord(payload)) {
    return null;
  }
  const error = payload["error"];
  if (!isRecord(error) || typeof error["code"] !== "string") {
    return null;
  }
  return {
    code: error["code"],
    message: typeof error["message"] === "string" ? error["message"] : "",
    retryable: error["retryable"] === true,
    details: Array.isArray(error["details"])
      ? error["details"].filter(isRecord).map((detail) => ({
          path: typeof detail["path"] === "string" ? detail["path"] : "",
          message: typeof detail["message"] === "string" ? detail["message"] : "",
        }))
      : [],
  };
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Narrowing helpers, so validators read as assertions rather than casts. */
export function requireRecord(value: unknown, what: string): Record<string, unknown> {
  if (!isRecord(value)) {
    throw new Error(`${what} was not an object`);
  }
  return value;
}

export function requireString(value: unknown, what: string): string {
  if (typeof value !== "string") {
    throw new Error(`${what} was not a string`);
  }
  return value;
}

export function optionalString(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

/**
 * A list of strings from an untrusted payload, tolerating absence.
 *
 * Absent and empty are treated alike on purpose: both mean "this finding covers
 * no extra paths", and a caller that had to distinguish them would be relying on
 * whether the server chose to emit an empty array. Non-string members are
 * dropped rather than thrown on — one malformed entry in a list of paths should
 * not take down the panel that renders the rest.
 */
export function stringList(value: unknown): readonly string[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.filter((entry): entry is string => typeof entry === "string");
}

export function requireArray(value: unknown, what: string): readonly unknown[] {
  if (!Array.isArray(value)) {
    throw new Error(`${what} was not an array`);
  }
  return value;
}
