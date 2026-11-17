// Copies vendored browser assets from node_modules into app/web/static/dist (ADR 0005).
// Nothing here is fetched at runtime from a CDN.
import { copyFileSync, mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const nm = join(root, "node_modules");
const dist = join(root, "app", "web", "static", "dist");

const copies = [
  ["htmx.org/dist/htmx.min.js", "vendor/htmx.min.js"],
  ["@alpinejs/csp/dist/cdn.min.js", "vendor/alpine-csp.min.js"],
  ["lucide-static/sprite.svg", "icons.svg"],
  ["@fontsource-variable/inter/files/inter-latin-wght-normal.woff2", "fonts/inter-latin.woff2"],
  ["@fontsource-variable/inter/files/inter-latin-ext-wght-normal.woff2", "fonts/inter-latin-ext.woff2"],
  ["@fontsource-variable/plus-jakarta-sans/files/plus-jakarta-sans-latin-wght-normal.woff2", "fonts/jakarta-latin.woff2"],
  ["@fontsource-variable/plus-jakarta-sans/files/plus-jakarta-sans-latin-ext-wght-normal.woff2", "fonts/jakarta-latin-ext.woff2"],
  ["@fontsource-variable/jetbrains-mono/files/jetbrains-mono-latin-wght-normal.woff2", "fonts/jetbrains-mono-latin.woff2"],
];

const own = [
  ["app/web/static/src/theme-init.js", "theme-init.js"],
  ["app/web/static/src/app.js", "app.js"],
];

for (const [from, to] of copies) {
  const target = join(dist, to);
  mkdirSync(dirname(target), { recursive: true });
  copyFileSync(join(nm, from), target);
}
for (const [from, to] of own) {
  const target = join(dist, to);
  mkdirSync(dirname(target), { recursive: true });
  copyFileSync(join(root, from), target);
}
console.log(`Copied ${copies.length + own.length} assets into app/web/static/dist`);
