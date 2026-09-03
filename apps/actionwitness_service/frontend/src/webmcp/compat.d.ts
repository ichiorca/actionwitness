/**
 * Tested compatibility augmentations ONLY (spec §25.12): keep this file limited
 * to gaps confirmed against the pinned `webmcp-types` version and the exact
 * tested Chrome build; delete entries when the pinned package covers them.
 * Runtime feature detection remains mandatory — types do not prove support.
 */
declare global {
  /**
   * `navigator.modelContext`, which the pinned `webmcp-types` does not declare —
   * it types the property on `Document` alone.
   *
   * This is a confirmed gap rather than a speculative one, which is the bar this
   * file sets. ADR-0002's spike (2026-08-31, Chrome 151 stable with
   * `#enable-webmcp-testing`) recorded the API present at **both**
   * `document.modelContext` and `navigator.modelContext`; the build attested
   * later exposed only the `document` location. Two host objects observed in two
   * builds of one browser is the reason the adapter resolves rather than assumes,
   * and this declaration is what lets it read the second location without a cast.
   *
   * Optional and `readonly`, matching how the package types the `Document` one:
   * the type says the property may be absent, and runtime feature detection —
   * not this declaration — is what proves support (§25.12).
   */
  interface Navigator {
    readonly modelContext?: WebMCP.ModelContext;
  }
}

export {};
