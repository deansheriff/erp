// Colour names match the Stitch exports so converted markup keeps its classes.
// Every colour is a CSS variable from app/web/static/src/tokens.css, so light and dark
// mode switch by changing variables, not by adding dark: variants (ADR 0005).
const token = (name) => `rgb(var(--kx-${name}) / <alpha-value>)`;

const modules = [
  "home", "hr", "payroll", "leave", "attendance", "expenses", "finance",
  "inventory", "procurement", "projects", "support", "fleet", "collaboration", "ai",
];

/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ["./app/**/*.html", "./app/**/*.py", "./app/web/static/src/**/*.js"],
  darkMode: ["selector", '[data-theme="dark"]'],
  // Status colours are chosen at render time (e.g. bg-{{ status }}-bg), so keep them all.
  safelist: [
    { pattern: /^(bg|text|border)-(success|warning|danger|info|neutral)-(bg|text)$/ },
    { pattern: /^kx-badge-(success|warning|danger|info|neutral|new)$/ },
    { pattern: /^module-[a-z]+$/ },
  ],
  theme: {
    extend: {
      colors: {
        canvas: token("canvas"),
        surface: token("surface"),
        "surface-sunken": token("surface-sunken"),
        "surface-raised": token("surface-raised"),
        line: token("line"),
        "line-strong": token("line-strong"),
        ink: token("ink"),
        "ink-2": token("ink-2"),
        "ink-3": token("ink-3"),
        iris: token("iris"),
        "iris-hover": token("iris-hover"),
        "iris-tint": token("iris-tint"),
        "on-iris": token("on-iris"),
        lime: token("lime"),
        "lime-ink": token("lime-ink"),
        focus: token("focus"),
        destructive: token("destructive"),
        "success-text": token("success-text"),
        "success-bg": token("success-bg"),
        "warning-text": token("warning-text"),
        "warning-bg": token("warning-bg"),
        "danger-text": token("danger-text"),
        "danger-bg": token("danger-bg"),
        "info-text": token("info-text"),
        "info-bg": token("info-bg"),
        "neutral-text": token("neutral-text"),
        "neutral-bg": token("neutral-bg"),
        hue: token("hue"),
        ...Object.fromEntries(modules.map((m) => [`mod-${m}`, token(`mod-${m}`)])),
      },
      fontFamily: {
        body: ["Inter", "system-ui", "sans-serif"],
        "body-sm": ["Inter", "system-ui", "sans-serif"],
        label: ["Inter", "system-ui", "sans-serif"],
        overline: ["Inter", "system-ui", "sans-serif"],
        display: ["Plus Jakarta Sans", "Inter", "sans-serif"],
        h1: ["Plus Jakarta Sans", "Inter", "sans-serif"],
        h2: ["Plus Jakarta Sans", "Inter", "sans-serif"],
        h3: ["Plus Jakarta Sans", "Inter", "sans-serif"],
        stat: ["Plus Jakarta Sans", "Inter", "sans-serif"],
        mono: ["JetBrains Mono", "ui-monospace", "monospace"],
        sans: ["Inter", "system-ui", "sans-serif"],
      },
      fontSize: {
        display: ["32px", { lineHeight: "40px", letterSpacing: "-0.015em", fontWeight: "700" }],
        h1: ["24px", { lineHeight: "32px", letterSpacing: "-0.015em", fontWeight: "700" }],
        h2: ["18px", { lineHeight: "28px", letterSpacing: "-0.015em", fontWeight: "600" }],
        h3: ["16px", { lineHeight: "24px", letterSpacing: "-0.015em", fontWeight: "600" }],
        stat: ["30px", { lineHeight: "36px", fontWeight: "700" }],
        body: ["16px", { lineHeight: "24px", fontWeight: "400" }],
        "body-sm": ["14px", { lineHeight: "20px", fontWeight: "400" }],
        label: ["14px", { lineHeight: "20px", fontWeight: "500" }],
        overline: ["12px", { lineHeight: "16px", letterSpacing: "0.06em", fontWeight: "600" }],
      },
      borderRadius: {
        DEFAULT: "0.25rem",
        lg: "0.5rem",
        control: "10px",
        xl: "0.75rem",
        "2xl": "1rem",
        sheet: "20px",
      },
      boxShadow: {
        card: "0 1px 2px rgb(22 22 42 / 0.04), 0 1px 1px rgb(22 22 42 / 0.03)",
        raised: "0 8px 24px -6px rgb(22 22 42 / 0.12), 0 2px 6px rgb(22 22 42 / 0.06)",
        overlay: "0 24px 48px -12px rgb(22 22 42 / 0.25)",
      },
      transitionTimingFunction: {
        out: "cubic-bezier(0.22, 1, 0.36, 1)",
        in: "cubic-bezier(0.4, 0, 1, 1)",
        standard: "cubic-bezier(0.2, 0, 0, 1)",
      },
      transitionDuration: { fast: "120ms", base: "200ms", slow: "320ms" },
      spacing: { 13: "3.25rem", 18: "4.5rem", 68: "17rem" },
    },
  },
  plugins: [require("@tailwindcss/forms")({ strategy: "class" })],
};
