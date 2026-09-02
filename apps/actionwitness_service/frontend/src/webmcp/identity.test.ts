/**
 * FR-169 — the identity hash a client sends with each invocation.
 *
 * The whole value of this module is that it agrees with
 * `packages/actionwitness_core/src/actionwitness_core/evidence/surface.py`. A
 * hash that agrees only with itself would refuse every honest invocation, which
 * is a worse outcome than the dead check it replaces — so agreement is asserted
 * two independent ways rather than described:
 *
 * 1. **The shared corpus.** `tests/fixtures/canonicalization/rfc8785_vectors.json`
 *    is the committed RFC 8785 corpus `actionwitness_core` is judged against
 *    (ADR-0004). This implementation is run against the same file, so a
 *    divergence in canonical *form* fails here before it can become a
 *    divergence in hashes.
 * 2. **Cross-language goldens.** The `sha256:` values below were produced by
 *    running `ToolDefinition.identity()` from that Python module over the same
 *    definitions, and are pinned here. If either side's rule changes, this fails
 *    — which is the point. Regenerate with:
 *
 *      python -c "import sys; sys.path.insert(0,'packages/actionwitness_core/src'); \
 *        from actionwitness_core.evidence.surface import ToolDefinition, ToolNamespace; \
 *        print(ToolDefinition(name='a', namespace=ToolNamespace.TARGET).identity())"
 *
 * The corpus test alone would not be enough: it proves the canonical text
 * agrees, not that the four sub-hashes are assembled from the same documents
 * under the same member names. The goldens alone would not be enough either —
 * they cover the shapes this product happens to register, and the corpus covers
 * the number, escaping and ordering edges that a hand-written canonicalizer gets
 * wrong. Each catches what the other misses.
 */

import { readFileSync } from "node:fs";
import path from "node:path";
import process from "node:process";

import { afterEach, describe, expect, it } from "vitest";

import { describeTool, readSurface } from "./adapter";
import {
  CanonicalizationError,
  canonicalSchema,
  canonicalText,
  contentHash,
  observedToolIdentityHash,
  toolIdentity,
} from "./identity";
import { type InstalledDouble, installModelContextDouble } from "../test/modelContextDouble";

let installed: InstalledDouble | null = null;

afterEach(() => {
  installed?.uninstall();
  installed = null;
});

// --- the corpus the core is judged against -----------------------------------

interface AcceptVector {
  readonly name: string;
  readonly input: unknown;
  readonly expected: string;
}

interface RejectVector {
  readonly name: string;
  readonly literal: string;
}

/**
 * The corpus, read from the repository rather than copied into this package.
 *
 * Copied vectors would drift the moment one side gained a case, and the two
 * implementations would then be judged against different evidence while both
 * suites stayed green — which is exactly the failure this file exists to catch.
 */
function loadVectors(): { accept: AcceptVector[]; reject: RejectVector[] } {
  // Resolved from the Vitest project root rather than from `import.meta.url`,
  // which Vite rewrites to an http URL under the jsdom environment.
  const corpus = path.resolve(
    process.cwd(),
    "../../../tests/fixtures/canonicalization/rfc8785_vectors.json",
  );
  const parsed: unknown = JSON.parse(readFileSync(corpus, "utf-8"));
  if (typeof parsed !== "object" || parsed === null) {
    throw new Error("the canonicalization corpus is not an object");
  }
  const record = parsed as Record<string, unknown>;
  const accept = record["accept"];
  const reject = record["reject"];
  if (!Array.isArray(accept) || !Array.isArray(reject)) {
    throw new Error("the canonicalization corpus has no accept/reject vectors");
  }
  return { accept: accept as AcceptVector[], reject: reject as RejectVector[] };
}

const VECTORS = loadVectors();

const NON_FINITE: Readonly<Record<string, number>> = {
  nan: Number.NaN,
  inf: Number.POSITIVE_INFINITY,
  "-inf": Number.NEGATIVE_INFINITY,
};

describe("canonicalText, against the corpus actionwitness_core is judged against", () => {
  it("has vectors to run, so an empty corpus cannot pass vacuously", () => {
    // Arrange / Act / Assert are the same statement here on purpose: this is the
    // guard on every `it.each` below, which would otherwise report success for a
    // file that failed to load a single case.
    expect(VECTORS.accept.length).toBeGreaterThan(5);
    expect(VECTORS.reject.length).toBeGreaterThan(0);
  });

  it.each(VECTORS.accept.map((vector) => [vector.name, vector] as const))(
    "produces the corpus's exact canonical text for %s",
    (_name, vector) => {
      const canonical = canonicalText(vector.input);

      expect(canonical).toBe(vector.expected);
    },
  );

  it.each(VECTORS.reject.map((vector) => [vector.name, vector] as const))(
    "refuses %s rather than encoding it",
    (_name, vector) => {
      const value = NON_FINITE[vector.literal];
      expect(value).toBeDefined();

      expect(() => canonicalText({ value })).toThrow(CanonicalizationError);
    },
  );
});

// --- the same identity the server computes -----------------------------------

/**
 * `update_cart` exactly as the browser now registers it (Appendix D.2).
 *
 * Written out rather than imported from the toolset, because the toolset builds
 * it inside a React hook — and because a golden that moved whenever the
 * registration moved would assert nothing about agreement with Python.
 */
const UPDATE_CART_SCHEMA = {
  type: "object",
  properties: {
    product_id: {
      type: "string",
      enum: ["mug-ceramic-001", "notebook-001", "tote-001"],
      description: "Stable seeded product id.",
    },
    quantity: {
      type: "integer",
      minimum: 0,
      maximum: 5,
      description: "Absolute quantity; zero removes the line.",
    },
    request_id: {
      type: "string",
      minLength: 8,
      maxLength: 80,
      description: "Idempotency key for this change.",
    },
  },
  required: ["product_id", "quantity", "request_id"],
  additionalProperties: false,
} as const;

const UPDATE_CART = {
  name: "update_cart",
  description: "Set or remove one cart line using a required request id.",
  read_only_hint: false,
  untrusted_content_hint: null,
  input_schema: UPDATE_CART_SCHEMA as unknown as Record<string, unknown>,
} as const;

/** Produced by `ToolDefinition.identity()` in actionwitness_core (see header). */
const CORE_UPDATE_CART_IDENTITY =
  "sha256:c6d79f6cc5793e8ed55bd4bb803d19b6b02fbb83485492a1568f4bf7ab293f69";
const CORE_UPDATE_CART_SCHEMA_HASH =
  "sha256:a9a5e2fc774c6bc9cbd95b73cc56ae2a336ec930b023395cfa14f1ecf9b7a16e";
const CORE_BARE_IDENTITY =
  "sha256:a4d49ab16abf04b56d72653dcbbe6b1c94a9d413bd19dde1bb1053fb90053399";
const CORE_BARE_HINTS_HASH =
  "sha256:e505afff0fe917ee8ff9c6eb1f60d859a7eb4e562cb978e70d70b26ba4449849";
const CORE_READ_ONLY_HINTS_HASH =
  "sha256:f9ec097ac6478b7bb3dfa6b5b0dec06adc816753ef5b6577758a170927a9fc75";

describe("toolIdentity agrees with actionwitness_core", () => {
  it("hashes the registered update_cart definition to the core's identity", async () => {
    const identity = await toolIdentity(UPDATE_CART);

    expect(identity.identityHash).toBe(CORE_UPDATE_CART_IDENTITY);
    expect(identity.schemaHash).toBe(CORE_UPDATE_CART_SCHEMA_HASH);
  });

  it("hashes a definition whose optional fields are all absent to the core's identity", async () => {
    // The other end of the range: `description` empty, both hints null, schema
    // empty. Those defaults live in the Python model, so a client that filled
    // them differently would disagree here and nowhere else.
    const identity = await toolIdentity({
      name: "a",
      description: "",
      read_only_hint: null,
      untrusted_content_hint: null,
      input_schema: {},
    });

    expect(identity.identityHash).toBe(CORE_BARE_IDENTITY);
    expect(identity.hintsHash).toBe(CORE_BARE_HINTS_HASH);
  });

  it("gives a reordered but equivalent schema the same identity", async () => {
    // §17.2's reason for canonicalising at all: `getTools()` promises no key
    // order and no `required`/`enum` order. Without this a browser that
    // enumerated an array differently than the capture did would be refused for
    // a schema it never changed.
    const shuffled = {
      ...UPDATE_CART,
      input_schema: {
        additionalProperties: false,
        required: ["request_id", "product_id", "quantity"],
        properties: {
          request_id: {
            maxLength: 80,
            type: "string",
            minLength: 8,
            description: "Idempotency key for this change.",
          },
          quantity: {
            maximum: 5,
            type: "integer",
            minimum: 0,
            description: "Absolute quantity; zero removes the line.",
          },
          product_id: {
            enum: ["tote-001", "mug-ceramic-001", "notebook-001"],
            type: "string",
            description: "Stable seeded product id.",
          },
        },
        type: "object",
      },
    };

    const identity = await toolIdentity(shuffled);

    expect(identity.identityHash).toBe(CORE_UPDATE_CART_IDENTITY);
  });

  it("changes the identity when the schema genuinely changes", async () => {
    // The counterfactual for the test above: canonicalisation must not be so
    // eager that it erases a real difference. The poisoned look-alike's extra
    // argument is the difference §13.3 depends on being visible.
    const poisoned = {
      ...UPDATE_CART,
      input_schema: {
        ...UPDATE_CART_SCHEMA,
        properties: { ...UPDATE_CART_SCHEMA.properties, redirect_to: { type: "string" } },
      },
    };

    const identity = await toolIdentity(poisoned);

    expect(identity.identityHash).not.toBe(CORE_UPDATE_CART_IDENTITY);
  });
});

// --- what makes hint_change reachable ----------------------------------------

describe("side-effect hints reach the hash", () => {
  it("moves the hints hash when a nested readOnlyHint flips, so hint_change can fire", async () => {
    // Arrange: the shape `getTools()` really reports — hints nested under
    // `annotations`. Reading the top level instead made every captured hint
    // `null`, which made the hints hash constant and `hint_change` a §9.5 delta
    // kind no run could ever produce while `one_mug_stable_surface` listed it
    // among the kinds that fail a run.
    installed = installModelContextDouble();
    await installed.modelContext.registerTool({
      name: "a",
      description: "",
      execute: async () => ({ ok: true }),
      annotations: { readOnlyHint: true },
    } as never);

    // Act: read the surface the way an invocation does, then flip the hint the
    // way a mid-run re-registration does and read again.
    const before = await readSurface();
    await installed.modelContext.registerTool({
      name: "a",
      description: "",
      execute: async () => ({ ok: true }),
      annotations: { readOnlyHint: false },
    } as never);
    const after = await readSurface();

    // Assert: the hints hash moved, and it moved to the values Python computes
    // for `read_only_hint: true` and `read_only_hint: false` respectively.
    const first = before?.[0];
    const second = after?.[0];
    expect(first).toBeDefined();
    expect(second).toBeDefined();
    const identityBefore = await toolIdentity(first as NonNullable<typeof first>);
    const identityAfter = await toolIdentity(second as NonNullable<typeof second>);

    expect(identityBefore.hintsHash).toBe(CORE_READ_ONLY_HINTS_HASH);
    expect(identityAfter.hintsHash).not.toBe(identityBefore.hintsHash);
    expect(identityAfter.identityHash).not.toBe(identityBefore.identityHash);
  });

  it("distinguishes an absent hint from an explicit false", async () => {
    // §9.11's reason for `null`: a tool that stopped *declaring* itself
    // read-only changed its hints, and a hash that coerced absence to `false`
    // would call that no change at all.
    const absent = await toolIdentity({
      name: "a",
      description: "",
      read_only_hint: null,
      untrusted_content_hint: null,
      input_schema: {},
    });
    const declared = await toolIdentity({
      name: "a",
      description: "",
      read_only_hint: false,
      untrusted_content_hint: null,
      input_schema: {},
    });

    expect(declared.hintsHash).not.toBe(absent.hintsHash);
  });
});

// --- what has no canonical form ----------------------------------------------

describe("values with no canonical form are refused, never guessed", () => {
  it("refuses an undefined member rather than dropping it like JSON.stringify", () => {
    // `JSON.stringify({ a: undefined })` is `{}`, so a definition with a member
    // removed would hash as one that never declared it.
    expect(() => canonicalText({ a: undefined })).toThrow(CanonicalizationError);
  });

  it("refuses a lone surrogate rather than escaping it into a valid hash", () => {
    // The core rejects these because they have no UTF-8 encoding. JSON.stringify
    // would happily escape one, producing a well-formed hash of something that
    // cannot be encoded — a hash that succeeds over corrupt input.
    expect(() => canonicalText({ a: "\ud800" })).toThrow(CanonicalizationError);
    // A properly paired astral character is not a lone surrogate.
    expect(canonicalText("\u{1f600}")).toBe('"\u{1f600}"');
  });

  it("refuses a $ref cycle rather than following one", () => {
    expect(() => canonicalSchema({ $ref: "#", type: "object" })).toThrow(CanonicalizationError);
  });

  it("refuses a schema nested past the bound rather than truncating it", () => {
    // Truncation would make a cut-off schema hash the same as a different one
    // that agreed down to the limit.
    let schema: Record<string, unknown> = { type: "string" };
    for (let depth = 0; depth < 40; depth += 1) {
      schema = { properties: schema };
    }

    expect(() => canonicalSchema(schema)).toThrow(CanonicalizationError);
  });

  it("refuses a non-finite number", async () => {
    await expect(contentHash({ n: Number.NaN })).rejects.toThrow(CanonicalizationError);
  });
});

// --- reading the identity at invocation time ---------------------------------

describe("observedToolIdentityHash", () => {
  it("hashes the definition the browser reports right now", async () => {
    // FR-169's "as observed at invocation time": read from the registry, not
    // from the literal this app registered — which is the only reading that can
    // ever disagree with the armed baseline.
    installed = installModelContextDouble();
    await installed.modelContext.registerTool({
      name: "update_cart",
      description: UPDATE_CART.description,
      inputSchema: UPDATE_CART_SCHEMA,
      annotations: { readOnlyHint: false },
      execute: async () => ({ ok: true }),
    } as never);

    const hash = await observedToolIdentityHash("update_cart");

    expect(hash).toBe(CORE_UPDATE_CART_IDENTITY);
  });

  it("sees a definition replaced since arming, with no toolchange required", async () => {
    // AC-25: a look-alike registered under a stable name must produce a
    // different hash, so the server refuses the call even when the surface
    // witness never saw the change.
    installed = installModelContextDouble();
    await installed.modelContext.registerTool({
      name: "update_cart",
      description: UPDATE_CART.description,
      inputSchema: UPDATE_CART_SCHEMA,
      annotations: { readOnlyHint: false },
      execute: async () => ({ ok: true }),
    } as never);
    await installed.modelContext.registerTool({
      name: "update_cart",
      description: UPDATE_CART.description,
      inputSchema: { ...UPDATE_CART_SCHEMA, properties: { redirect_to: { type: "string" } } },
      annotations: { readOnlyHint: false },
      execute: async () => ({ ok: true }),
    } as never);

    const hash = await observedToolIdentityHash("update_cart");

    expect(hash).not.toBeNull();
    expect(hash).not.toBe(CORE_UPDATE_CART_IDENTITY);
  });

  it("returns null when this browser has no WebMCP", async () => {
    // §15.3 keeps the field optional for this case: a client that cannot
    // compute one must still be able to invoke, and the surface capture already
    // fails `stable_tool_surface` closed for a run with no baseline.
    await expect(observedToolIdentityHash("update_cart")).resolves.toBeNull();
  });

  it("returns null for a tool the registry does not report", async () => {
    // It vanished between registration and this call — an `added`/`removed`
    // delta for the surface policy to judge, not a hash to invent.
    installed = installModelContextDouble();

    await expect(observedToolIdentityHash("update_cart")).resolves.toBeNull();
  });

  it("returns null rather than throwing for a definition with no canonical form", async () => {
    // A hostile registration must not be able to break every invocation by
    // registering something unhashable.
    installed = installModelContextDouble();
    await installed.modelContext.registerTool({
      name: "hostile",
      description: "",
      inputSchema: { $ref: "#" },
      execute: async () => ({ ok: true }),
    } as never);
    // Guard: the descriptor really does reach the capture, so this is testing
    // the hash refusing rather than the capture dropping it.
    expect(describeTool({ name: "hostile", inputSchema: { $ref: "#" } })).not.toBeNull();

    await expect(observedToolIdentityHash("hostile")).resolves.toBeNull();
  });
});
