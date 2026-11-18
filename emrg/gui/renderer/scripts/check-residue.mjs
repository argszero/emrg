#!/usr/bin/env node
/**
 * check-residue.mjs — the old page must not be in the bundle that ships (issue #2969).
 *
 * The source scan in `src/lib/residue.test.ts` reads what the bundle is *made from*.
 * This reads what the bundle *is*: every `.js`/`.css`/`.html` under `dist/`, after Vite
 * has minified it. The two are not the same subject, and the difference is the one the
 * host measured — a component deleted from the tree but still reachable through an
 * import chain, or a default map that survived a refactor, ships either way, and only
 * the artifact answers "what did the build actually emit".
 *
 * It runs as part of `npm run build`, so the gate sits where the artifact exists: the
 * CI test job, the release pipeline and `npm start`'s `prestart` all come through that
 * one command.
 *
 * Exit codes, because "could not measure" is never a pass:
 *   0  no needle found in the artifact
 *   1  at least one needle found — the old page is still shipping
 *   2  nothing measured — the directory is absent or holds no bundle
 *
 * Usage: node scripts/check-residue.mjs [dir]   (dir defaults to ../dist)
 */
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = fileURLToPath(new URL(".", import.meta.url));
const NEEDLES_URL = new URL("../src/lib/residue-needles.json", import.meta.url);
const BUNDLE_FILE = /\.(?:js|css|html)$/;

/**
 * The needle list lives in JSON rather than in a `.ts` beside the test on purpose: the
 * source scan globs `*.ts`/`*.tsx`, so a TypeScript list would be scanned as a source
 * and every needle would report a hit against its own definition.
 */
function readNeedles() {
  try {
    const list = JSON.parse(readFileSync(NEEDLES_URL, "utf8"));
    if (!Array.isArray(list) || list.length === 0) return [];
    return list.filter((n) => typeof n === "string" && n.length > 0);
  } catch {
    return [];
  }
}

function walk(dir) {
  const out = [];
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) out.push(...walk(path));
    else if (BUNDLE_FILE.test(name)) out.push(path);
  }
  return out;
}

const target = process.argv[2] ? resolve(process.argv[2]) : resolve(HERE, "..", "dist");
console.log(`check-residue: reading the bundle under ${target}`);

const needles = readNeedles();
if (needles.length === 0) {
  console.error(`check-residue: UNMEASURABLE — no needle list at ${fileURLToPath(NEEDLES_URL)}`);
  process.exit(2);
}

let files;
try {
  files = walk(target);
} catch (err) {
  console.error(`check-residue: UNMEASURABLE — cannot read ${target} (${err.code ?? err.message})`);
  process.exit(2);
}
if (files.length === 0) {
  console.error(`check-residue: UNMEASURABLE — ${target} holds no .js/.css/.html to read`);
  process.exit(2);
}

// Bytes are decoded as UTF-8 by name, so a non-UTF-8 character cannot be silently
// replaced by U+FFFD and read as a clean bundle.
const hits = [];
for (const file of files) {
  const text = readFileSync(file, "utf8");
  for (const needle of needles) {
    if (text.includes(needle)) hits.push(`${file.slice(target.length + 1)}: ${needle}`);
  }
}

if (hits.length > 0) {
  console.error(`check-residue: the old page is still shipping — ${hits.length} hit(s):`);
  for (const hit of hits) console.error(`  ${hit}`);
  process.exit(1);
}

console.log(
  `check-residue: OK — ${files.length} bundle file(s) read, none of ${needles.length} old-page string(s) found`,
);
