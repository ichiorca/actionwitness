/**
 * The flat, declarative contract-instantiation form (§6.3, §25.2, FR-021).
 *
 * This is the third WebMCP registration mechanism and the only one with no
 * registration call: the browser reads `toolname` off this `<form>` and the
 * tool exists because the markup does. The agent's affordance and the person's
 * affordance are the same DOM node, which is the whole point — a hand-written
 * schema can drift from the form it claims to describe, and this one cannot.
 *
 * Every WebMCP attribute and event lives in `webmcp/adapter.ts`. This component
 * receives prop objects and a submit handler and never learns an attribute
 * name (constitution §1).
 *
 * ## What this form deliberately cannot do
 *
 * FR-021: "the declarative form shall never accept nested assertions, policies,
 * paths, or arbitrary JSON". There are four controls, all scalar, and the
 * server expands a trusted template from them. A person cannot author an
 * assertion here and neither can an agent — which is exactly why the same form
 * can be handed to both.
 *
 * ## Why the unaccepted controls are disabled rather than hidden
 *
 * A template allowlists its own scalars: `confirmed_checkout_only` says nothing
 * about quantity. Removing the control when that template is selected would
 * change the declarative tool's shape as the selection changed, so an agent
 * that read the form once could hold a schema the page no longer offers.
 * Disabling keeps the surface stable, keeps the reason visible to the person,
 * and keeps the value out of the submission — a disabled control contributes
 * nothing to `FormData`.
 *
 * The server re-checks the allowlist regardless. What is disabled here is a
 * courtesy; what the server refuses is the rule.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { ApiError } from "../api/client";
import {
  type ContractTemplateSummary,
  type CreatedContract,
  createOutcomeContract,
} from "../api/contracts";
import {
  toolAutoSubmitProps,
  toolParameterProps,
  useDeclarativeTool,
} from "../webmcp/adapter";

export const CREATE_CONTRACT_TOOL = "create_outcome_contract";

const TOOL_DESCRIPTION =
  "Create an immutable outcome contract by expanding one trusted built-in template " +
  "with flat scalar parameters. This tool cannot author arbitrary assertions or policies.";

/** §25.2's controls. Named once so the markup and the reader agree. */
const QUANTITY = "quantity";
const DISCOUNT_CODE = "discount_code";

export interface ContractFormProps {
  readonly templates: readonly ContractTemplateSummary[];
  /** Called with the created contract so the workspace can select it. */
  readonly onCreated: (contract: CreatedContract) => void;
}

type Submission =
  | { readonly kind: "idle" }
  | { readonly kind: "submitting" }
  | { readonly kind: "created"; readonly contract: CreatedContract }
  | {
      readonly kind: "refused";
      readonly message: string;
      readonly fields: ReadonlyMap<string, string>;
    };

/**
 * §15.8's details, keyed by the control they name.
 *
 * The server sends `quantity` for a rejected expansion and `body.quantity` for
 * a rejected body shape — two boundaries, one envelope — so the last path
 * segment is what identifies the control. Rendered as text, never as markup.
 */
function fieldErrorsOf(error: ApiError): ReadonlyMap<string, string> {
  const fields = new Map<string, string>();
  for (const detail of error.envelope?.details ?? []) {
    const segments = detail.path.split(".");
    const control = segments[segments.length - 1];
    if (control !== undefined && control !== "" && !fields.has(control)) {
      fields.set(control, detail.message);
    }
  }
  return fields;
}

export function ContractForm({ templates, onCreated }: ContractFormProps): React.ReactElement {
  const first = templates[0];
  const [templateId, setTemplateId] = useState<string>(first?.sourceTemplateId ?? "");
  const [submission, setSubmission] = useState<Submission>({ kind: "idle" });

  /**
   * Adopt the first template once the list arrives.
   *
   * `templates` is empty on the first render — `App` fetches it — so the
   * initialiser above always ran with `""`, and nothing ever revised it. The
   * result was a form that *looked* correct and was not: the `<select>` fell
   * back to displaying option zero, while `selected` stayed `undefined`, so
   * every parameter read as unaccepted. Quantity and Discount sat disabled
   * under the words "This template does not use a quantity" — for a template
   * that does — and a submission would have posted an empty `template_id`.
   *
   * Only when the current value names nothing. A person who has chosen a
   * template keeps it; this is the empty-to-loaded transition, not a sync.
   */
  useEffect(() => {
    if (templateId === "" && first !== undefined) {
      setTemplateId(first.sourceTemplateId);
    }
  }, [first, templateId]);

  const selected = useMemo(
    () => templates.find((template) => template.sourceTemplateId === templateId),
    [templates, templateId],
  );
  const accepts = useCallback(
    (parameter: string): boolean => selected?.parameters.includes(parameter) ?? false,
    [selected],
  );

  /**
   * One handler for both callers.
   *
   * §25.2 requires the declarative path to "post the same payload to FastAPI
   * used by a human submission". The way to guarantee that is to have one
   * function rather than two that are supposed to agree — so an agent and a
   * person run this identical code, and the only difference is who receives
   * the returned value.
   */
  const submit = useCallback(
    async (values: FormData): Promise<unknown> => {
      setSubmission({ kind: "submitting" });
      const text = (field: string): string | undefined => {
        const value = values.get(field);
        return typeof value === "string" && value !== "" ? value : undefined;
      };
      try {
        const contract = await createOutcomeContract({
          templateId: text("template_id") ?? "",
          contractName: text("contract_name"),
          quantity: text(QUANTITY),
          discountCode: text(DISCOUNT_CODE),
        });
        setSubmission({ kind: "created", contract });
        onCreated(contract);
        return contract;
      } catch (error: unknown) {
        if (error instanceof ApiError) {
          setSubmission({
            kind: "refused",
            message: error.message,
            fields: fieldErrorsOf(error),
          });
        } else {
          setSubmission({
            kind: "refused",
            message: "The contract could not be created.",
            fields: new Map(),
          });
        }
        // Rethrown so an agent's call fails rather than resolving with nothing.
        // A tool that reported success on a refused submission would be the
        // false self-report this product exists to catch.
        throw error;
      }
    },
    [onCreated],
  );

  const binding = useDeclarativeTool(
    { name: CREATE_CONTRACT_TOOL, description: TOOL_DESCRIPTION },
    submit,
  );

  const refused = submission.kind === "refused" ? submission : null;
  const fieldError = (control: string): string | undefined => refused?.fields.get(control);

  return (
    <section className="panel" aria-label="Create a contract">
      <h3>Create a contract</h3>
      <p>
        Pick a built-in template and set its scalar values. The server expands the template;
        this form cannot author assertions or policies.
      </p>

      {binding.activity === "activated" ? (
        <p role="status">An agent is filling in this form.</p>
      ) : null}
      {binding.activity === "cancelled" ? (
        <p role="status">The agent cancelled its submission. Nothing was created.</p>
      ) : null}

      <form {...binding.formProps} ref={binding.ref} onSubmit={binding.onSubmit}>
        <p>
          <label htmlFor="contract-template">Template</label>
          <select
            id="contract-template"
            name="template_id"
            value={templateId}
            onChange={(event) => {
              setTemplateId(event.target.value);
            }}
            {...toolParameterProps("Built-in template to instantiate.")}
          >
            {templates.map((template) => (
              <option key={template.sourceTemplateId} value={template.sourceTemplateId}>
                {template.name}
              </option>
            ))}
          </select>
        </p>
        {fieldError("template_id") === undefined ? null : (
          <p className="panel__error">{fieldError("template_id")}</p>
        )}

        <p>
          <label htmlFor="contract-name">Name (optional)</label>
          <input
            id="contract-name"
            name="contract_name"
            type="text"
            maxLength={80}
            {...toolParameterProps("Optional display name for the immutable contract.")}
          />
        </p>
        {fieldError("contract_name") === undefined ? null : (
          <p className="panel__error">{fieldError("contract_name")}</p>
        )}

        <p>
          <label htmlFor="contract-quantity">Quantity</label>
          <input
            id="contract-quantity"
            name={QUANTITY}
            type="number"
            min={1}
            max={5}
            step={1}
            disabled={!accepts(QUANTITY)}
            aria-describedby="contract-quantity-note"
            {...toolParameterProps("Absolute target quantity used by the selected template.")}
          />
          <span id="contract-quantity-note">
            {accepts(QUANTITY)
              ? "Between 1 and 5."
              : "This template does not use a quantity."}
          </span>
        </p>
        {fieldError(QUANTITY) === undefined ? null : (
          <p className="panel__error">{fieldError(QUANTITY)}</p>
        )}

        <p>
          <label htmlFor="contract-discount">Discount code</label>
          <select
            id="contract-discount"
            name={DISCOUNT_CODE}
            defaultValue=""
            disabled={!accepts(DISCOUNT_CODE)}
            aria-describedby="contract-discount-note"
            {...toolParameterProps("Allowlisted discount used by templates that require one.")}
          >
            <option value="">Template default</option>
            <option value="SAVE20">SAVE20</option>
          </select>
          <span id="contract-discount-note">
            {accepts(DISCOUNT_CODE)
              ? "Only allowlisted codes are accepted."
              : "This template does not use a discount."}
          </span>
        </p>
        {fieldError(DISCOUNT_CODE) === undefined ? null : (
          <p className="panel__error">{fieldError(DISCOUNT_CODE)}</p>
        )}

        <button
          type="submit"
          disabled={submission.kind === "submitting" || templates.length === 0}
          {...toolAutoSubmitProps()}
        >
          {submission.kind === "submitting" ? "Creating…" : "Create contract"}
        </button>
      </form>

      <p role="status">
        {submission.kind === "created"
          ? `Created "${submission.contract.name}" (${submission.contract.contentHash}).`
          : null}
        {refused === null ? null : refused.message}
      </p>
    </section>
  );
}
