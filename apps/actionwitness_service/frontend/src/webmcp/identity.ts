/**
 * The identity of a tool definition, as observed at invocation time (FR-169).
 *
 * FR-169: "each recorded target-tool invocation shall carry the identity hash of
 * the tool definition as observed at invocation time; a mismatch shall fail the
 * policy even if no `toolchange` event was observed." AC-25 is the case it
 * exists for — a definition altered between capture and invocation, with the
 * `toolchange` suppressed or missed, must still be refused.
 *
 * The server has carried that check since 005: `:invoke` accepts an optional
 * `tool_identity_hash` and refuses on mismatch. It was never exercised outside
 * tests, because no client sent one. This module is what makes the field real.
 *
 * ## Why the browser is allowed to compute this, when it computes no capture hash
 *
 * `surface.ts` deliberately sends the server *definitions* and no hashes, and
 * says why: a page that hashed its own capture would be the tool surface
 * vouching for its own integrity. Nothing here weakens that, because the two
 * hashes point in opposite directions.
 *
 * - A **capture** hash would be the page asserting what the evidence is. The
 *   server would have to believe it. That is the category error.
 * - An **invocation** hash is the page declaring what it *thinks* it is calling.
 *   The server recomputes the armed baseline's identity itself and compares. A
 *   client that lies, miscomputes, or is running an attacker's canonicalisation
 *   produces a value that does not match, and the invocation is refused. The
 *   only direction this can fail is closed.
 *
 * So the server still owns the answer; this side only owns the question.
 *
 * ## One hashing rule, restated — not a second one invented
 *
 * `packages/actionwitness_core/.../evidence/surface.py` is the authority, and
 * everything below mirrors it deliberately rather than approximately: RFC 8785
 * canonical JSON (§17.2, ADR-0004), SHA-256, §17.2's four sub-hashes, and the
 * schema canonicalisation that sorts the order-insensitive JSON Schema keywords.
 * `identity.test.ts` runs this implementation against
 * `tests/fixtures/canonicalization/rfc8785_vectors.json` — the *same committed
 * corpus* `actionwitness_core` is judged against — so "the two agree" is a
 * property under test rather than a claim in a comment.
 *
 * Three of RFC 8785's four hard parts are free in this language, which is not a
 * coincidence: JCS is *defined* in terms of ECMAScript.
 *
 * - Member ordering is by UTF-16 code unit (§3.2.3), which is exactly what
 *   `Array.prototype.sort` does to strings by default.
 * - Number formatting is ECMAScript `Number::toString` (§3.2.2.3), which is
 *   exactly `String(value)`.
 * - String escaping (§3.2.2.2) is exactly `JSON.stringify` of a string.
 *
 * The Python side had to write all three out by hand, and ADR-0004 records the
 * four independent ways a naive port gets them wrong. Here the risk runs the
 * other way — the primitives are right, so the only way to get this wrong is to
 * *stop* using them.
 *
 * ## What is refused rather than guessed
 *
 * Every refusal below exists because the alternative is a well-formed hash
 * describing something other than the definition it came from: a non-finite
 * number, a lone surrogate, an `undefined` member JSON has no form for, a
 * `$ref` cycle whose expansion does not terminate, and nesting past the bound
 * the walk needs to terminate at all. A refusal here is recoverable — the
 * caller omits the field and the surface policy still judges the capture — while
 * a wrong hash would refuse an honest invocation.
 */

import { readSurface, type CapturedTool } from "./adapter";

/** Nesting `canonicalText` accepts from untrusted input; mirrors the core's. */
export const MAX_CANONICAL_DEPTH = 100;

/** Nesting a tool input schema may reach before canonicalisation refuses it. */
export const MAX_TOOL_SCHEMA_DEPTH = 32;

/**
 * A value that has no canonical form, so no hash may be derived from it.
 *
 * Its own class rather than a bare `Error`: the caller's decision — omit the
 * field and invoke anyway — is correct for this and wrong for a network fault,
 * and the two arrive at the same `catch`.
 */
export class CanonicalizationError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "CanonicalizationError";
  }
}

/** JSON Schema keywords whose array value is a *set*, not a sequence (§17.2). */
const UNORDERED_SCALAR_KEYWORDS: ReadonlySet<string> = new Set(["required", "enum"]);

/** Keywords whose array members are schemas and are likewise unordered. */
const UNORDERED_SCHEMA_KEYWORDS: ReadonlySet<string> = new Set(["anyOf", "oneOf", "allOf"]);

/** Pointers that name the document root — the cycle hand-written schemas have. */
const SELF_REFERENTIAL: ReadonlySet<string> = new Set(["#", "#/"]);

const encoder = new TextEncoder();

// --- RFC 8785 canonical JSON -------------------------------------------------

/** The RFC 8785 canonical form of a value, as text. */
export function canonicalText(value: unknown): string {
  return encode(value, "$", 0);
}

/**
 * The RFC 8785 canonical form as UTF-8 bytes.
 *
 * Hashing consumes these rather than a re-decoded string, so no encoding step
 * sits between what was canonicalized and what was hashed.
 */
export function canonicalBytes(value: unknown): Uint8Array {
  return encoder.encode(canonicalText(value));
}

function encode(value: unknown, location: string, depth: number): string {
  if (depth > MAX_CANONICAL_DEPTH) {
    // A bound rather than a stack overflow. It is also what terminates a
    // reference cycle, which no size bound would.
    throw new CanonicalizationError(
      `nesting deeper than ${MAX_CANONICAL_DEPTH} levels is refused at ${location}`,
    );
  }
  if (value === null) {
    return "null";
  }
  switch (typeof value) {
    case "boolean":
      return value ? "true" : "false";
    case "number":
      return encodeNumber(value, location);
    case "string":
      return encodeString(value, location);
    case "object":
      break;
    default:
      // `undefined`, functions, symbols and bigints. `JSON.stringify` drops the
      // first three from an object silently, which would hash a definition with
      // a member removed as though the member had never been declared.
      throw new CanonicalizationError(
        `${typeof value} has no JSON representation at ${location}`,
      );
  }

  if (isArray(value)) {
    const items = value.map((item, index) => encode(item, `${location}[${index}]`, depth + 1));
    return `[${items.join(",")}]`;
  }
  if (!isRecord(value)) {
    // A Date, Map, or class instance. The core refuses anything that is not a
    // Mapping or a Sequence for the same reason: its JSON form would be this
    // language's opinion rather than the value.
    throw new CanonicalizationError(
      `${value.constructor.name} has no JSON representation at ${location}`,
    );
  }

  // §3.2.3 orders members by UTF-16 code unit, which is what `sort()` does to
  // strings with no comparator. The Python side has to encode each key as
  // big-endian UTF-16 to get the same order.
  const members = Object.keys(value)
    .sort()
    .map((key) => {
      const encoded = encode(value[key], `${location}.${key}`, depth + 1);
      return `${encodeString(key, location)}:${encoded}`;
    });
  return `{${members.join(",")}}`;
}

/**
 * §3.2.2.3 — serialize a double by ECMAScript's own `Number::toString`.
 *
 * `String(value)` *is* that algorithm, so the exponent thresholds, the shortest
 * round-tripping digits, and the unpadded exponent are all correct by
 * construction. Only two cases need saying out loud: a non-finite number has no
 * canonical form at all, and `-0` must emit `0` so two values that compare equal
 * produce one hash.
 */
function encodeNumber(value: number, location: string): string {
  if (!Number.isFinite(value)) {
    throw new CanonicalizationError(
      `a non-finite number has no JSON representation and no canonical form at ${location}`,
    );
  }
  return value === 0 ? "0" : String(value);
}

/**
 * §3.2.2.2 — the minimal escaping, which is `JSON.stringify`'s.
 *
 * The lone-surrogate check has to come first, and it is the one place this
 * cannot lean on the platform. `JSON.stringify` escapes an unpaired surrogate
 * into a well-formed `\udXXX`, so an ill-formed string would produce a perfectly
 * valid hash of something that has no UTF-8 encoding — exactly the "hash that
 * succeeds over corrupt input" the core module refuses.
 */
function encodeString(value: string, location: string): string {
  if (hasLoneSurrogate(value)) {
    throw new CanonicalizationError(
      `a lone surrogate has no UTF-8 encoding and cannot be canonicalized at ${location}`,
    );
  }
  return JSON.stringify(value);
}

function hasLoneSurrogate(value: string): boolean {
  for (let index = 0; index < value.length; index += 1) {
    const unit = value.charCodeAt(index);
    if (unit >= 0xd800 && unit <= 0xdbff) {
      const trailing = value.charCodeAt(index + 1);
      // `charCodeAt` past the end is NaN, and NaN fails both comparisons.
      if (!(trailing >= 0xdc00 && trailing <= 0xdfff)) {
        return true;
      }
      index += 1;
    } else if (unit >= 0xdc00 && unit <= 0xdfff) {
      return true;
    }
  }
  return false;
}

// --- SHA-256 content hashing -------------------------------------------------

/**
 * §17.2's `sha256:...` content hash of a value's canonical bytes.
 *
 * Asynchronous because `SubtleCrypto` is, and `SubtleCrypto` because the
 * alternative is shipping a hand-written SHA-256 into a bundle — custom
 * cryptography, which the constitution puts behind operator approval and which
 * would be a second implementation of something the platform already has.
 *
 * `crypto.subtle` is absent outside a secure context. That is a refusal rather
 * than a fallback: a page served over plain HTTP has no business computing an
 * identity nobody can trust anyway.
 */
export async function contentHash(value: unknown): Promise<string> {
  const subtle: SubtleCrypto | undefined = globalThis.crypto?.subtle;
  if (subtle === undefined) {
    throw new CanonicalizationError(
      "this context has no SubtleCrypto, so no content hash can be computed",
    );
  }
  const digest = await subtle.digest("SHA-256", canonicalBytes(value));
  return `sha256:${toHex(digest)}`;
}

function toHex(buffer: ArrayBuffer): string {
  return Array.from(new Uint8Array(buffer), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

// --- §17.2 schema canonicalisation -------------------------------------------

/**
 * §17.2's normative schema canonicalisation, mirroring the core's.
 *
 * RFC 8785 sorts object keys but preserves array order, while a JSON Schema is
 * order-insensitive for `required`, `enum`, `anyOf` and `oneOf`. Without
 * normalising those, two semantically identical schemas hash differently — and
 * on this side that would refuse an honest invocation because a browser
 * enumerated an array differently than the capture did.
 *
 * Like the core, this normalises no keyword *defaults*: inventing one would
 * erase a real difference between two schemas, while a missed normalisation only
 * over-reports, which is visible.
 */
export function canonicalSchema(schema: Record<string, unknown>): unknown {
  return canonicalizeSchemaValue(schema, 0);
}

function canonicalizeSchemaValue(value: unknown, depth: number): unknown {
  if (depth > MAX_TOOL_SCHEMA_DEPTH) {
    // Refused rather than truncated: a schema silently cut off at a depth limit
    // would hash the same as a different schema that agreed down to that depth.
    throw new CanonicalizationError(
      `a tool input schema may nest at most ${MAX_TOOL_SCHEMA_DEPTH} levels; ` +
        "deeper input is refused rather than truncated",
    );
  }

  if (isArray(value)) {
    // Every array the vocabulary does not name is ordered — `prefixItems`, and
    // anything unknown. Order is preserved because for those it is meaning.
    return value.map((item) => canonicalizeSchemaValue(item, depth + 1));
  }
  if (!isRecord(value)) {
    return value;
  }
  if (hasRefCycle(value)) {
    // Rejected rather than followed: following one does not terminate, and
    // guessing a depth limit would make the hash depend on the limit.
    throw new CanonicalizationError(
      "a tool input schema contains a $ref cycle and cannot be canonicalised",
    );
  }

  const result: Record<string, unknown> = {};
  for (const key of Object.keys(value)) {
    const item = value[key];
    if (UNORDERED_SCALAR_KEYWORDS.has(key) && isArray(item)) {
      // Sorted by canonical bytes so a mixed-type `enum` orders totally rather
      // than by whatever `sort()` would make of the coerced strings.
      result[key] = [...item].sort(byCanonicalBytes);
    } else if (UNORDERED_SCHEMA_KEYWORDS.has(key) && isArray(item)) {
      result[key] = item
        .map((member) => canonicalizeSchemaValue(member, depth + 1))
        .sort(byCanonicalBytes);
    } else {
      result[key] = canonicalizeSchemaValue(item, depth + 1);
    }
  }
  return result;
}

/**
 * Order two members by their canonical UTF-8 bytes.
 *
 * Bytes rather than JavaScript's string comparison, which orders by UTF-16 code
 * unit. The two disagree above the BMP — an astral character sorts *before* a
 * high BMP one by code unit and *after* it by UTF-8 — and the core sorts by
 * bytes. An enum containing an emoji would otherwise hash differently on the two
 * sides, which is the silent divergence this whole module is written to avoid.
 */
function byCanonicalBytes(left: unknown, right: unknown): number {
  return compareBytes(canonicalBytes(left), canonicalBytes(right));
}

function compareBytes(left: Uint8Array, right: Uint8Array): number {
  const shared = Math.min(left.length, right.length);
  for (let index = 0; index < shared; index += 1) {
    const difference = (left[index] ?? 0) - (right[index] ?? 0);
    if (difference !== 0) {
      return difference;
    }
  }
  return left.length - right.length;
}

/**
 * Whether a `$ref` in this schema points at an ancestor of itself.
 *
 * Only local pointers are examined, as in the core: a `$ref` to another document
 * is not a cycle this can see, and pretending otherwise would refuse a schema
 * for a property it does not have.
 */
function hasRefCycle(schema: Record<string, unknown>): boolean {
  const targets = new Set<string>();
  const seen = new Set<object>();

  const walk = (value: unknown): void => {
    if (typeof value !== "object" || value === null || seen.has(value)) {
      return;
    }
    seen.add(value);
    if (isArray(value)) {
      for (const item of value) {
        walk(item);
      }
      return;
    }
    if (!isRecord(value)) {
      return;
    }
    for (const key of Object.keys(value)) {
      const item = value[key];
      if (key === "$ref" && typeof item === "string") {
        targets.add(item);
      } else {
        walk(item);
      }
    }
  };

  walk(schema);
  for (const target of targets) {
    if (SELF_REFERENTIAL.has(target)) {
      return true;
    }
  }
  return false;
}

// --- §17.2's tool identity ---------------------------------------------------

/**
 * §17.2's four sub-hashes plus their composite, named as the core names them.
 *
 * Four rather than one whole-definition hash, because §9.5 grades the delta
 * kinds differently — a description edit is a warning, a schema mutation is a
 * failure — and a single hash could only report that *something* changed. The
 * comparison this side feeds is the composite, but the parts are exposed because
 * `hints_hash` moving is precisely what makes `hint_change` reachable, and a
 * test that could only see the composite could not say which kind it proved.
 */
export interface ToolIdentity {
  readonly nameHash: string;
  readonly descriptionHash: string;
  readonly hintsHash: string;
  readonly schemaHash: string;
  readonly identityHash: string;
}

/**
 * §17.2's identity for one captured definition.
 *
 * The hashed documents use the core's own snake_case member names, because those
 * names are inside the hash: `read_only_hint` and `readOnlyHint` are different
 * bytes, and this side agreeing with itself is not the property that matters.
 */
export async function toolIdentity(tool: CapturedTool): Promise<ToolIdentity> {
  const nameHash = await contentHash(tool.name);
  const descriptionHash = await contentHash(tool.description);
  const hintsHash = await contentHash({
    read_only_hint: tool.read_only_hint,
    untrusted_content_hint: tool.untrusted_content_hint,
  });
  const schemaHash = await contentHash(canonicalSchema(tool.input_schema));
  const identityHash = await contentHash({
    name_hash: nameHash,
    description_hash: descriptionHash,
    hints_hash: hintsHash,
    schema_hash: schemaHash,
  });
  return { nameHash, descriptionHash, hintsHash, schemaHash, identityHash };
}

/**
 * The identity hash of the tool named, as the browser reports it right now
 * (FR-169's "as observed at invocation time").
 *
 * Read through the adapter's `readSurface`, which is the product's one
 * `getTools()` call — so this sees the same registry the surface witness
 * captures from, and an injection that moved the definition between arming and
 * this moment is visible here whether or not its `toolchange` was observed.
 *
 * `null` — rather than a throw — in every case where no honest hash exists: no
 * WebMCP, no such tool in the registry, no secure context, or a definition with
 * no canonical form. The server documents `tool_identity_hash` as optional for
 * exactly this reason ("a client that cannot compute one must still be able to
 * invoke"), and the surface capture still reaches `stable_tool_surface`
 * independently, so silence here narrows the evidence rather than removing it.
 * Refusing to invoke instead would be this page inventing a policy the server
 * does not have, and would make an un-instrumented browser unable to act at all.
 */
export async function observedToolIdentityHash(name: string): Promise<string | null> {
  try {
    const surface = await readSurface();
    if (surface === null) {
      return null;
    }
    const tool = surface.find((entry) => entry.name === name);
    if (tool === undefined) {
      // The tool vanished between registration and this call. That is a
      // `removed` delta for the surface policy to judge, not a hash to invent.
      return null;
    }
    return (await toolIdentity(tool)).identityHash;
  } catch {
    return null;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * `Array.isArray`, but narrowing to `unknown[]` rather than `any[]`.
 *
 * TypeScript's own signature widens an `unknown` to `any[]`, which switches off
 * every check on the members — in a module whose entire input is an untrusted
 * descriptor, that is the one narrowing that must not be lossy.
 */
function isArray(value: unknown): value is readonly unknown[] {
  return Array.isArray(value);
}
