import { describe, expect, it } from "vitest";

/**
 * residue.test.ts — the old page must be gone from what ships (issue #2969).
 *
 * Deleting a component is not the same as deleting the page it drew: a string cannot
 * be unmounted, and the copy the host saw is exactly the kind that survives a refactor
 * inside a stylesheet, a dictionary or a default map. So the check runs over the tree
 * the bundle is built from, and names each string the old page owned. A hit is not a
 * style problem — it is the old page still reachable.
 *
 * The old page's fingerprints:
 *   - its grouping labels — the section label above the two-level session list;
 *   - its results panel's empty state, the English sentence shown when a session had
 *     produced no files yet;
 *   - its tab names, which are the tabs it kept after the inspector replaced it.
 */

/**
 * Every renderer source the bundle is built from, read through Vite rather than Node.
 *
 * The renderer typechecks with `types: ["vite/client", ...]` and has no `@types/node`
 * of its own, so a `node:fs` read here compiles on a host whose sibling package hoists
 * that library and fails on a clean `npm ci` — `tests/test_renderer_node_types.py` is
 * the guard that says so. `?raw` is the same reading without the dependency.
 */
const SOURCES = import.meta.glob(["../**/*.ts", "../**/*.tsx"], {
  query: "?raw",
  eager: true,
  import: "default",
}) as Record<string, string>;

const SHIPPED = Object.entries(SOURCES)
  .filter(([path]) => !/\.test\.tsx?$/.test(path))
  .sort(([a], [b]) => a.localeCompare(b));

/**
 * The strings the old left column and the old right panel are made of.
 *
 * The list lives in a JSON file so that `scripts/check-residue.mjs` — the half that
 * reads the built bundle instead of the sources — reads the same one. A `.ts` file
 * would not do: the glob below matches every `*.ts`, so a TypeScript list would be
 * scanned as a source and each needle would report a hit against its own definition.
 */
import RESIDUE from "./residue-needles.json";

describe("the old page's fingerprints", () => {
  it("源代码里一处都不剩", () => {
    // 一个空集合会把这个测试变成一次空比较，那不是一次测量
    expect(SHIPPED.length).toBeGreaterThan(20);
    const hits: string[] = [];
    for (const [file, text] of SHIPPED) {
      for (const needle of RESIDUE) {
        if (text.includes(needle)) hits.push(`${file.replace("../", "")}: ${needle}`);
      }
    }
    expect(hits).toEqual([]);
  });
});
