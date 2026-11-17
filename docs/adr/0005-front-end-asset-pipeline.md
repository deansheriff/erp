# 0005. Front-end asset pipeline: Tailwind 3.4, vendored HTMX/Alpine, self-hosted fonts

- Status: Accepted
- Date: 2026-11-17

## Context

Screens are generated in Google Stitch, whose HTML exports use Tailwind v3 utility
classes and a v3-style `tailwind.config` theme. The app is server-rendered (Jinja2 +
HTMX + Alpine.js), and must serve a strict Content-Security-Policy with no third-party
script or font hosts at runtime.

## Decision

- **Tailwind CSS 3.4** built with the npm package (`npm run build:css`). The design
  tokens live once, as CSS custom properties in `app/web/static/src/tokens.css`, and
  `tailwind.config.js` maps Tailwind colour names to those variables. Light and dark modes
  switch by changing variables, not classes.
- **HTMX and Alpine.js** come from npm and are copied into `app/web/static/dist/vendor/` by
  `npm run build:vendor`. No CDN at runtime. Alpine uses its **CSP build**, so every
  component is registered in `app.js` with `Alpine.data` and templates use only simple
  property and method references.
- **Fonts** (Plus Jakarta Sans, Inter, JetBrains Mono) come from `@fontsource` npm
  packages and are served from our own origin.
- **Icons:** Lucide, through an SVG sprite generated at build time from the `lucide-static`
  npm package.
- The Docker image builds assets in a Node stage, and only the built files ship.
  `app/web/static/dist/` is git-ignored.
- When converting a Stitch export, the CDN `<script>` and Google Fonts `<link>` tags are
  removed, and any hard-coded hex colours are replaced with token classes.

## Consequences

Stitch output converts with minimal changes. CSP can be `script-src 'self'`. Node is
needed only at build time, never in production.
