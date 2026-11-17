"""Generate docs/SCREENS.md from design/stitch/screens.yaml.

uv run python scripts/gen_screens_doc.py          write the file
uv run python scripts/gen_screens_doc.py --check  fail if the file is out of date
"""

import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = ROOT / "design" / "stitch" / "screens.yaml"
OUTPUT = ROOT / "docs" / "SCREENS.md"


def render(registry: dict[str, Any]) -> str:
    lines = [
        "# Screens",
        "",
        "<!-- Generated from design/stitch/screens.yaml by scripts/gen_screens_doc.py."
        " Do not edit by hand. -->",
        "",
        f"Stitch project `{registry['stitch_project']}`, design system"
        f" `{registry['design_system']}`.",
        "",
    ]
    screens = registry["screens"]
    for phase in sorted({s["phase"] for s in screens}):
        lines += [f"## Phase {phase}", ""]
        lines += [
            "| Screen | Route | Permission | Field classes | Approved by |",
            "|---|---|---|---|---|",
        ]
        for s in (s for s in screens if s["phase"] == phase):
            classes = ", ".join(s.get("field_classes") or []) or "—"
            lines.append(
                f"| **{s['title']}** (`{s['id']}`) | `{s['route']}` | {s['permission']} "
                f"| {classes} | {s.get('approved_by') or 'pending'} |"
            )
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    registry = yaml.safe_load(REGISTRY.read_text(encoding="utf-8"))
    content = render(registry)
    if "--check" in sys.argv:
        current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
        if current != content:
            print("docs/SCREENS.md is out of date. Run: uv run python scripts/gen_screens_doc.py")
            return 1
        return 0
    OUTPUT.write_text(content, encoding="utf-8", newline="\n")
    print(f"Wrote {OUTPUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
