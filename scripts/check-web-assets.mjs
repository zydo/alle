import { spawnSync } from "node:child_process";
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const assets = join(root, "src", "alle", "assets");
const files = readdirSync(assets).filter((name) => name.endsWith(".js"));
const failures = [];

for (const name of files) {
  const path = join(assets, name);
  const syntax = spawnSync(process.execPath, ["--check", path], { encoding: "utf8" });
  if (syntax.status !== 0) failures.push(`${name}: ${syntax.stderr.trim()}`);
  const source = readFileSync(path, "utf8");
  for (const match of source.matchAll(/(?:from\s+|import\s*)["'](\.\.?\/[^"']+)["']/g)) {
    const target = resolve(dirname(path), match[1]);
    if (!existsSync(target)) failures.push(`${name}: unresolved import ${match[1]}`);
  }
  for (const sink of ["document.write", "insertAdjacentHTML", "eval(", "new Function("]) {
    if (source.includes(sink)) failures.push(`${name}: unsafe HTML/code sink ${sink}`);
  }
  if (/\.outerHTML\s*=/.test(source)) failures.push(`${name}: unsafe outerHTML assignment`);
}

if (failures.length) {
  console.error(failures.join("\n"));
  process.exit(1);
}
console.log(`web static checks passed (${files.length} modules)`);
