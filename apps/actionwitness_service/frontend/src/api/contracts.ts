/**
 * The contract template listing and instantiation calls (§15.2, FR-021).
 *
 * `parameters` is the only field here that changes how the UI behaves: it says
 * which flat controls a template accepts, so the form can render exactly those.
 * It is a convenience and never the enforcement — `POST /contracts` re-checks
 * the allowlist, because a browser deciding which fields are legal would be the
 * client authorizing its own input.
 *
 * Both payloads arrive as `unknown` and are narrowed here. The server is ours,
 * but the response still crosses a boundary.
 */

import {
  isRecord,
  optionalString,
  request,
  requireArray,
  requireRecord,
  requireString,
  stringList,
} from "./client";

export interface ContractTemplateSummary {
  readonly contractId: string;
  readonly sourceTemplateId: string;
  readonly name: string;
  readonly description: string | null;
  readonly targetId: string | null;
  /** The scalars this template allowlists, in the order the server sends. */
  readonly parameters: readonly string[];
}

export interface CreatedContract {
  readonly contractId: string;
  readonly name: string;
  readonly contentHash: string;
  readonly sourceTemplateId: string;
}

/** The flat form's fields (§25.2). Absent means "not supplied", never `""`.
 *
 * `quantity` admits a string on purpose. A form field's value *is* a string,
 * and text that does not parse as a number is not silently dropped here — it is
 * sent, and the server rejects it with a field-level error naming `quantity`.
 * Discarding it in the browser would leave a person staring at a control they
 * filled in and a contract created as though they had not.
 */
export interface ContractDraft {
  readonly templateId: string;
  readonly contractName?: string | undefined;
  readonly quantity?: number | string | undefined;
  readonly discountCode?: string | undefined;
}

function parseTemplate(value: unknown): ContractTemplateSummary {
  const record = requireRecord(value, "a contract template");
  return {
    contractId: requireString(record["contract_id"], "contract_id"),
    sourceTemplateId: requireString(record["source_template_id"], "source_template_id"),
    name: requireString(record["name"], "name"),
    description: optionalString(record["description"]),
    targetId: optionalString(record["target_id"]),
    parameters: stringList(record["parameters"]),
  };
}

export function parseTemplates(value: unknown): readonly ContractTemplateSummary[] {
  const record = requireRecord(value, "the template listing");
  return requireArray(record["templates"], "templates").filter(isRecord).map(parseTemplate);
}

export function parseCreatedContract(value: unknown): CreatedContract {
  const record = requireRecord(value, "the created contract");
  return {
    contractId: requireString(record["contract_id"], "contract_id"),
    name: requireString(record["name"], "name"),
    contentHash: requireString(record["content_hash"], "content_hash"),
    sourceTemplateId: requireString(record["source_template_id"], "source_template_id"),
  };
}

export async function listContractTemplates(
  signal?: AbortSignal,
): Promise<readonly ContractTemplateSummary[]> {
  return request("/contracts/templates", { parse: parseTemplates, signal });
}

/**
 * Create one contract from a template (FR-021).
 *
 * Only the fields the person actually filled in are sent. An omitted control
 * has to reach the server as *absent* rather than as `null` or `""`: the
 * expansion treats absence as "use the template's own value", and an empty
 * string would be a value the template must then reject.
 */
export async function createOutcomeContract(
  draft: ContractDraft,
  signal?: AbortSignal,
): Promise<CreatedContract> {
  return request("/contracts", {
    method: "POST",
    body: {
      template_id: draft.templateId,
      ...(draft.contractName === undefined ? {} : { contract_name: draft.contractName }),
      ...(draft.quantity === undefined ? {} : { quantity: draft.quantity }),
      ...(draft.discountCode === undefined ? {} : { discount_code: draft.discountCode }),
    },
    parse: parseCreatedContract,
    signal,
  });
}
