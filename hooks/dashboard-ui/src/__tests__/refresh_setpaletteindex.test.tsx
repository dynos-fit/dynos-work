/**
 * Regression test for AC1 of task-20260616-004.
 *
 * Verifies that the 'r'/'R' keyboard refresh handler in CommandPalette.tsx
 * calls setPaletteIndex(data) within the same .then() block that assigns
 * paletteCache = data.
 *
 * WHY source-analysis: the refresh handler uses a floating async fetch chain
 * inside a keydown listener registered in useEffect. The floating promise is
 * not flushable into the rendered DOM under jsdom+RTL (documented in
 * .dynos/task-20260616-001/evidence/tdd-tests-repair.md; the AC8 tests there
 * use the same source-analysis approach). A behavioral render test cannot
 * observe the result, so we inspect the source directly — the same pattern
 * used for AC8 and AC10 in bugfix.test.tsx.
 */

import { describe, it, expect, vi } from "vitest";

describe("AC1 (task-20260616-004) — 'r' refresh handler calls setPaletteIndex after paletteCache = data", () => {
  let src: string;

  // Read the source once using vi.importActual (same pattern as AC8/AC10 in bugfix.test.tsx).
  // vi.mock("node:fs") is hoisted file-wide in bugfix.test.tsx only; here we
  // have no top-level vi.mock, so vi.importActual gives us the real fs module.
  beforeEach(async () => {
    const fs = await vi.importActual<typeof import("node:fs")>("node:fs");
    const path = await vi.importActual<typeof import("node:path")>("node:path");
    src = (fs as typeof import("node:fs")).readFileSync(
      (path as typeof import("node:path")).resolve(
        __dirname,
        "../components/CommandPalette.tsx"
      ),
      "utf8"
    );
  });

  it("refresh handler block contains both paletteCache = data and setPaletteIndex( in the same .then(", () => {
    /**
     * The bug (before fix): the 'r' handler assigned paletteCache = data in
     * the second .then() but never called setPaletteIndex(), so React state
     * was never updated and the results list remained empty after refresh.
     *
     * The fix: the second .then() must contain BOTH:
     *   paletteCache = data;
     *   setPaletteIndex(data);
     *
     * We locate the refresh handler block, extract the .then( that contains
     * 'paletteCache = data', and assert setPaletteIndex( appears within it.
     */

    // Locate the 'r' keydown handler block — identified by the 'r'/'R' key
    // guard and the paletteCache = null reset preceding the fetch chain.
    const handlerStart = src.indexOf("paletteCache = null");
    expect(handlerStart).toBeGreaterThan(-1, "refresh handler must reset paletteCache to null");

    // Find the first .then( after the paletteCache = null reset
    const thenStart = src.indexOf(".then((data:", handlerStart);
    expect(thenStart).toBeGreaterThan(-1, "refresh handler must have a .then((data:...) arm after paletteCache = null");

    // Find the closing brace of that .then( block — it ends before the next
    // chained .catch( call. We extract the substring between .then( and .catch(
    const catchAfterThen = src.indexOf(".catch(", thenStart);
    expect(catchAfterThen).toBeGreaterThan(-1, ".then( must be followed by a .catch(");

    const thenBlock = src.slice(thenStart, catchAfterThen);

    // Assert paletteCache = data is inside this .then( block
    expect(thenBlock).toContain("paletteCache = data");

    // Assert setPaletteIndex( is called inside the SAME .then( block
    expect(thenBlock).toMatch(/setPaletteIndex\s*\(/);
  });

  it("setPaletteIndex( call in refresh .then( receives 'data' as its argument", () => {
    /**
     * Stronger assertion: the call is setPaletteIndex(data), not some other
     * argument, ensuring the fetched index is committed to React state.
     */
    const handlerStart = src.indexOf("paletteCache = null");
    expect(handlerStart).toBeGreaterThan(-1);

    const thenStart = src.indexOf(".then((data:", handlerStart);
    expect(thenStart).toBeGreaterThan(-1);

    const catchAfterThen = src.indexOf(".catch(", thenStart);
    expect(catchAfterThen).toBeGreaterThan(-1);

    const thenBlock = src.slice(thenStart, catchAfterThen);

    // Must contain setPaletteIndex(data) — the exact argument that was missing
    expect(thenBlock).toMatch(/setPaletteIndex\s*\(\s*data\s*\)/);
  });

  it("paletteCache = data assignment appears before setPaletteIndex( in the same .then( block", () => {
    /**
     * Order guard: the fix must assign the module-level cache first, then
     * commit to React state. This matches the loadIndex() pattern in the
     * first-open handler (paletteCache = ...; setPaletteIndex(paletteCache)).
     */
    const handlerStart = src.indexOf("paletteCache = null");
    expect(handlerStart).toBeGreaterThan(-1);

    const thenStart = src.indexOf(".then((data:", handlerStart);
    expect(thenStart).toBeGreaterThan(-1);

    const catchAfterThen = src.indexOf(".catch(", thenStart);
    expect(catchAfterThen).toBeGreaterThan(-1);

    const thenBlock = src.slice(thenStart, catchAfterThen);

    const cacheAssignPos = thenBlock.indexOf("paletteCache = data");
    const setPaletteIndexPos = thenBlock.search(/setPaletteIndex\s*\(/);

    expect(cacheAssignPos).toBeGreaterThan(-1);
    expect(setPaletteIndexPos).toBeGreaterThan(-1);
    // paletteCache assignment comes first (or at same position is impossible —
    // they are different statements)
    expect(cacheAssignPos).toBeLessThan(setPaletteIndexPos);
  });
});
