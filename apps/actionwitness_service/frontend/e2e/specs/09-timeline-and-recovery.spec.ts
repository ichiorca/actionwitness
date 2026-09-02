/**
 * §15.3's polling, against a real network that can actually fail.
 *
 * `useRunTimeline` has three properties the unit test asserts with fake timers
 * and a stubbed `fetch`: it stops on a terminal run status rather than on an
 * empty page, it drops a response that arrives out of order, and it cancels in
 * flight when the component goes. Fake timers prove the logic; they cannot prove
 * the page survives a connection that really drops and really comes back, or
 * that a reload rebuilds the timeline from sequence zero.
 *
 * The offline test in particular is only possible here. It is also the one a
 * user is most likely to hit.
 */

import { MUG, UPDATE_CART, VERIFY_OUTCOME, expect, test } from "../support/harness";

test.describe("the run timeline", () => {
  test("appends events in sequence order as the agent acts", async ({ workspace, agent }) => {
    await workspace.open();
    await workspace.arm();

    const entries = workspace.timeline.locator("ol.timeline li");
    await expect(entries.first()).toBeVisible();

    await agent.call(UPDATE_CART, {
      product_id: MUG,
      quantity: 1,
      request_id: "e2e-timeline-order",
    });

    // Arming, the initial snapshot, the invocation's start and its terminal
    // event: the timeline is the record, so it has to show both halves of the
    // invocation rather than only its result.
    await expect(
      workspace.timeline.locator("li[data-event-type='run_armed']"),
    ).toHaveCount(1);
    await expect(
      workspace.timeline.locator("li[data-event-type='tool_invocation_started']"),
    ).toHaveCount(1);
    await expect(
      workspace.timeline.locator("li[data-event-type='tool_invocation_completed']"),
    ).toHaveCount(1);

    const sequences = await entries.evaluateAll((nodes) =>
      nodes.map((node) => Number(node.querySelector(".timeline__sequence")?.textContent?.slice(1))),
    );
    expect(sequences).toEqual([...sequences].sort((a, b) => a - b));
    expect(new Set(sequences).size).toBe(sequences.length);
  });

  test("stops polling on a terminal run rather than on an empty page", async ({
    workspace,
    agent,
  }) => {
    await workspace.open();
    await workspace.arm();

    // While the run is live the page says it is still watching, even though
    // most polls return nothing new. `has_more: false` means "nothing right
    // now", and a client that stopped there would miss the events a failing run
    // is judged by.
    await expect(workspace.timeline).toContainText("watching for new activity");

    await agent.call(UPDATE_CART, {
      product_id: MUG,
      quantity: 1,
      request_id: "e2e-timeline-terminal",
    });
    await agent.call(VERIFY_OUTCOME);
    await workspace.expectTerminalPhase();

    // The verdict is not this test's subject — the default contract expects a
    // discount this journey never applies — so the assertion is that the watch
    // ended, on the run's own status.
    await expect(workspace.timeline).not.toContainText("watching for new activity");
    await expect(workspace.timeline).toContainText(
      /Run is (passed|passed_with_warnings|failed|error|cancelled)\./,
    );
  });

  test("reports a lost connection and recovers when it returns", async ({
    workspace,
    agent,
    page,
  }) => {
    await workspace.open();
    await workspace.arm();
    await agent.call(UPDATE_CART, {
      product_id: MUG,
      quantity: 1,
      request_id: "e2e-timeline-offline",
    });
    const before = await workspace.timeline.locator("ol.timeline li").count();
    expect(before).toBeGreaterThan(0);

    // A real dropped connection, which no fake timer can produce.
    await page.context().setOffline(true);

    // Said out loud. `useRunTimeline` always computed this and nothing rendered
    // it, so a lost connection looked exactly like a quiet run: the events
    // froze, the banner still said the page was watching, and there was no way
    // to tell the difference.
    await expect(workspace.timeline.getByRole("alert")).toContainText("could not be read");
    await expect(workspace.timeline.getByRole("alert")).toContainText("keeps retrying");

    // The events already on screen stay: a failed poll must not rewind the
    // record in front of the user, and must not clear it either.
    await expect(workspace.timeline.locator("ol.timeline li")).toHaveCount(before);

    await page.context().setOffline(false);
    // The chained timeout keeps trying, so recovery needs no reload — and the
    // notice goes when it succeeds, or it would outlive the problem.
    await expect(workspace.timeline.getByRole("alert")).toHaveCount(0);
    await agent.call(VERIFY_OUTCOME);
    await workspace.expectTerminalPhase();
  });

  test("rebuilds the whole timeline after a reload", async ({ workspace, agent, page }) => {
    await workspace.open();
    await workspace.arm();
    await agent.call(UPDATE_CART, {
      product_id: MUG,
      quantity: 1,
      request_id: "e2e-timeline-reload",
    });
    const before = await workspace.timeline.locator("ol.timeline li").count();

    // The cursor lives in the page, not on the server: "everything after N" is
    // a position this page holds, so a reload starts from zero and must replay
    // the whole record rather than an empty one.
    //
    // A lower bound rather than equality: the harness keeps appending its own
    // events — surface captures, guidance transitions — while the reload
    // happens, and a strict count would assert that nothing else was recorded
    // in the meantime, which is a race rather than a requirement.
    await page.reload();
    await expect(
      workspace.page.getByRole("heading", { name: "ActionWitness", level: 1 }),
    ).toBeVisible();
    await expect
      .poll(async () => await workspace.timeline.locator("ol.timeline li").count(), {
        message: "waiting for the reloaded page to replay the timeline",
      })
      .toBeGreaterThanOrEqual(before);
    // Replayed from the first event, not resumed from wherever the last page
    // had got to.
    await expect(workspace.timeline.locator("li[data-event-type='run_armed']")).toHaveCount(1);
    await expect(
      workspace.timeline.locator("li[data-event-type='tool_invocation_completed']"),
    ).toHaveCount(1);
  });
});

test.describe("recovery", () => {
  test("returns the workspace to a ready state and keeps the contract", async ({
    workspace,
    agent,
    harness,
  }) => {
    await workspace.open();
    await workspace.arm();
    await agent.call(UPDATE_CART, {
      product_id: MUG,
      quantity: 1,
      request_id: "e2e-recovery-cart",
    });
    await workspace.expectPhase("running");

    const before = String((await harness.workspace())["selected_contract_id"]);
    await workspace.reset();

    // FR-013: cancel what is in flight, keep what is finished, retain the
    // contract. A reset that dropped the contract would send the operator back
    // to the start of a journey they were halfway through.
    await workspace.expectPhase("contract_ready");
    expect(String((await harness.workspace())["selected_contract_id"])).toBe(before);
    await expect(workspace.panel("Target")).toContainText("Run: none");
  });

  test("cancels the in-flight run rather than leaving it open", async ({
    workspace,
    agent,
    harness,
  }) => {
    await workspace.open();
    await workspace.arm();
    await workspace.expectPhase("armed");
    const active = (await harness.workspace())["active_run"];
    expect(active, "the run must exist before it can be cancelled").not.toBeNull();
    const runId = String((active as Record<string, unknown>)["id"]);
    await agent.call(UPDATE_CART, {
      product_id: MUG,
      quantity: 1,
      request_id: "e2e-recovery-cancel",
    });

    await workspace.reset();
    await workspace.expectPhase("contract_ready");

    const cancelled = await harness.run(runId);
    expect(cancelled["status"]).toBe("cancelled");
    // The cancellation is on the record, with a reason: a partially completed
    // operation stays visible rather than disappearing.
    const types = ((await harness.events(runId))["events"] as Record<string, unknown>[]).map(
      (event) => event["event_type"],
    );
    expect(types).toContain("run_cancelled");
  });
});
