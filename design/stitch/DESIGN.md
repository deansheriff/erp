# Kxmeleon ERP: Design System "Chroma"

> This file is uploaded to the Stitch project "Kxmeleon ERP" (`upload_design_md`), and
> the design system is created from it (`create_design_system_from_design_md`). Every
> screen is generated against that design system. The same tokens are compiled into
> `app/web/static/src/tokens.css` and `tailwind.config.js`, so Stitch exports and the
> implemented templates share one source.

## 1. Concept

**Chroma**: a calm, modern workspace that *adapts its colour to where you are*, like the
chameleon the product is named after. The chrome is quiet ink and soft lavender-grey.
Each module carries its own hue, which tints the active navigation, the page header glow
and the module's charts. Moving from Payroll (emerald) to Expenses (tangerine), the
interface shifts colour gently. The core brand pair is **Iris** (a confident blue-violet)
for actions and **Lime** (an electric yellow-green) as a sparing highlight.

**Feel:** modern SaaS, light and airy, with generous whitespace, soft layered surfaces,
rounded geometry and crisp typography. It stays dense and precise where the work is
(tables, ledgers, payroll), and stays friendly where everyone goes (self-service, mobile).
Motion is subtle and purposeful: things *settle* into place rather than bounce.

**Users:** finance and HR staff working for hours at a desk; every employee for short
bursts on a phone (apply for leave, snap a receipt, check a payslip, approve a request).

## 2. Voice and formats

- Plain British English as used in Nigerian business ("organisation", "authorise").
- Direct, warm and calm. Say what happened and what to do next.
  - "Claim EXP-0142 sent to Adaeze Okafor for approval."
  - "You can't approve your own claim. Ask another approver."
  - Never "Oops!", no exclamation marks, never blame the user.
- Use the terms Nigerian HR and finance staff use: PAYE, pension (RSA PIN, PFA), NHF,
  NSITF, ITF, BVN, NIN, TIN, RC number, State IRS.
- Buttons name the action: "Submit claim", "Approve", "Post payroll". Never "OK" or "Yes".
- **Money:** `₦1,250,000.00`. Negatives in parentheses: `(₦1,250.00)`. Always 2 decimal
  places, tabular numerals, right-aligned in tables. Currency always shown; NGN default.
- **Dates:** `17 Nov 2026`. Date and time: `17 Nov 2026, 14:05 WAT`. 24-hour clock.
- **Document numbers** in monospace: `INV-2026-000142`.

## 3. Colour

All text pairs are verified at **≥ 4.5:1**, and UI boundaries and focus indicators at
**≥ 3:1** (checked by script, WCAG 2.2 AA).

### 3.1 Light mode

| Token | Hex | Use |
|---|---|---|
| `canvas` | `#F6F6FA` | App background, a very faint lavender-grey |
| `surface` | `#FFFFFF` | Cards, tables, sidebar, modals |
| `surface-sunken` | `#EFEFF6` | Table header, filter bar, read-only fields, inputs at rest |
| `surface-raised` | `#FFFFFF` + shadow `raised` | Dropdowns, popovers, toasts |
| `line` | `#E4E4EE` | Hairline dividers and card outlines (decorative only) |
| `line-strong` | `#8B8BA6` | Input, checkbox and radio boundaries (3.3:1) |
| `ink` | `#16162A` | Primary text (16.5:1) |
| `ink-2` | `#4B4B66` | Labels, secondary text (8.4:1) |
| `ink-3` | `#626280` | Hints, timestamps, placeholders (5.9:1). Never lighter. |
| `iris` | `#5A4BDB` | Primary buttons, links, selection, focus ring (6.1:1) |
| `iris-hover` | `#4A3CC4` | Hover/pressed primary (7.7:1 with white) |
| `iris-tint` | `#EEECFD` | Selected rows, active chips; text on it is `iris-hover` (6.6:1) |
| `on-iris` | `#FFFFFF` | Text on iris |
| `lime` | `#C5F04A` | Highlight fill only: "new" pills, the current step, the unread dot, the KPI spark. Text on lime is always `ink` (13.5:1). |
| `lime-ink` | `#4D7A0A` | Lime used as text or icon on white (5.1:1) |

### 3.2 Dark mode

| Token | Hex |
|---|---|
| `canvas` | `#0B0B12` |
| `surface` | `#14141F` |
| `surface-sunken` | `#1C1C2B` |
| `line` | `#262638` |
| `line-strong` | `#6C6C88` |
| `ink` / `ink-2` / `ink-3` | `#ECECF4` / `#B8B8CE` / `#9A9AB4` |
| `iris` | `#A89EFF` (text on it `#15122E`, 7.8:1) |
| `iris-hover` | `#BDB5FF` |
| `iris-tint` | `#221F44` |
| `lime` | `#D2F57A` |
| `focus` | `#B4ABFF` |

Dark mode follows the OS by default; users can override it in their profile menu
(Light / Dark / System). Every screen is generated and checked in both modes.

### 3.3 Status colours

Status is **always a word plus colour, never colour alone**.

| Status | Light text / bg | Dark text / bg | Used for |
|---|---|---|---|
| success | `#0F6B3E` / `#DDF7E8` | `#6FE3A6` / `#11281D` | approved, paid, posted, ready, active |
| warning | `#8A4B00` / `#FFF1D6` | `#FFC970` / `#2D2110` | pending, due soon, scanning, on leave |
| danger | `#B4232F` / `#FFE4E6` | `#FF9AA6` / `#33151B` | rejected, overdue, virus found, terminated |
| info | `#1D55B8` / `#DFEBFF` | `#94BBFF` / `#142240` | submitted, in review, notices |
| neutral | `#4B4B66` / `#EFEFF6` | `#C4C4D8` / `#22222F` | draft, cancelled, closed, archived |

Destructive buttons: `#C42836` with white text (5.7:1).

### 3.4 Module hues (the "chameleon" layer)

Each module has a hue. Within that module, the hue appears in:
- the sidebar's active-item indicator and module icon tile;
- a soft radial **glow** behind the page header (hue at 10% opacity, fading to
  transparent over 480px);
- the module dashboard's chart series;
- the 2px progress bar under the top bar during HTMX requests.

It **never** colours body text, buttons or status. The module name always accompanies
its hue.

| Module | Light | Dark |
|---|---|---|
| Home / Admin | `#3F3F5A` | `#B4B4CC` |
| HR | `#7C3AED` violet | `#C4B5FD` |
| Payroll | `#047857` emerald | `#6EE7B7` |
| Leave | `#0369A1` ocean | `#7DD3FC` |
| Attendance | `#0E7490` lagoon | `#67E8F9` |
| Expenses | `#C2410C` tangerine | `#FDBA74` |
| Finance | `#1D4ED8` cobalt | `#93C5FD` |
| Inventory | `#92400E` amber-brown | `#FCD34D` |
| Procurement | `#4D7C0F` olive | `#BEF264` |
| Projects | `#0F766E` teal | `#5EEAD4` |
| Support | `#BE185D` raspberry | `#F9A8D4` |
| Fleet | `#44403C` graphite | `#D6D3D1` |
| Collaboration | `#A21CAF` orchid | `#F0ABFC` |
| AI Assistant | `#6D28D9` with an iris-to-lime gradient tile | `#DDD6FE` |

All light hues are ≥ 5:1 on white, and all dark hues ≥ 9:1 on dark `surface`.

## 4. Typography

- **Display and headings:** Plus Jakarta Sans 600/700, letter-spacing -0.015em.
- **UI and body:** Inter 400/500/600, with tabular numerals
  (`font-variant-numeric: tabular-nums`) for every number in tables, totals and forms.
- **Code and document numbers:** JetBrains Mono 500.
- Body text is 16px on every device; dense tables may use 14px; nothing below 12px.

| Style | Size / line | Font, weight | Use |
|---|---|---|---|
| `display` | 32 / 40 | Jakarta 700 | Dashboard greeting ("Good morning, Fatima") |
| `h1` | 24 / 32 | Jakarta 700 | Page title |
| `h2` | 18 / 28 | Jakarta 600 | Card and section title |
| `h3` | 16 / 24 | Jakarta 600 | Sub-section |
| `body` | 16 / 24 | Inter 400 | Default text, inputs |
| `body-sm` | 14 / 20 | Inter 400 | Table cells, secondary text |
| `label` | 14 / 20 | Inter 500 | Labels, buttons |
| `overline` | 12 / 16 | Inter 600, +0.06em, uppercase | Table headers, nav group labels |
| `stat` | 30 / 36 | Jakarta 700, tabular | KPI tiles |

## 5. Shape, space and depth

- **Radii:** 8px (badges, checkboxes, small chips); 10px (inputs, buttons); 16px (cards,
  tables, dropdowns); 20px (modals, drawers, bottom sheets); full (avatars, pills,
  segmented controls).
- **Spacing (4px base):** 4, 8, 12, 16, 20, 24, 32, 40, 48, 64. Card padding is 24px on
  desktop and 16px on mobile. Page gutter is 16 / 24 / 32px (mobile / tablet / desktop).
  Content max width is 1440px; forms max out at 720px.
- **Touch targets:** at least **44×44px** for every interactive element (icon buttons,
  row actions, checkbox hit areas, tabs, pagination).
- **Control height:** 44px. Compact 36px is allowed only in desktop table toolbars, with
  the hit area padded to 44px.
- **Depth:** soft, ink-tinted shadows. Surfaces float rather than sit on borders.
  - `card`: `0 1px 2px rgb(22 22 42 / 0.04), 0 1px 1px rgb(22 22 42 / 0.03)` plus a 1px `line` border.
  - `raised`: `0 8px 24px -6px rgb(22 22 42 / 0.12), 0 2px 6px rgb(22 22 42 / 0.06)`.
  - `overlay`: `0 24px 48px -12px rgb(22 22 42 / 0.25)`, over a scrim of
    `rgb(11 11 18 / 0.45)` with a 4px backdrop blur.
  - Dark mode replaces shadows with a lighter `line` and a 1px inner top highlight
    `rgb(255 255 255 / 0.04)`.
- **Glass, used sparingly:** the sticky top bar is `surface` at 80% opacity with a 12px
  backdrop blur, so content scrolls softly underneath. Nothing else is translucent.
- **Icons:** Lucide, 20px in UI and 16px inline, 1.75 stroke, rounded caps. Icon-only
  buttons always have an accessible name and a tooltip.

## 6. Motion

Motion is subtle, fast and meaningful. It explains where things came from and where they
went. It never delays the user.

| Token | Value | Use |
|---|---|---|
| `dur-fast` | 120ms | Hover, press, colour changes, checkbox ticks |
| `dur-base` | 200ms | Dropdowns, tooltips, toasts, tab indicator |
| `dur-slow` | 320ms | Modals, drawers, page-section entrances |
| `ease-out` | `cubic-bezier(0.22, 1, 0.36, 1)` | Entrances (things settle) |
| `ease-in` | `cubic-bezier(0.4, 0, 1, 1)` | Exits (faster than entrances) |
| `ease-standard` | `cubic-bezier(0.2, 0, 0, 1)` | Moving and resizing |

Signature behaviours:

1. **Page arrival:** cards fade in and rise 8px, staggered 40ms apart (at most 6 items
   staggered; the rest appear together).
2. **HTMX swaps** use the View Transitions API: the old content fades out over 120ms
   and the new content fades in and rises 4px over 200ms. A 2px module-hue progress
   bar runs under the top bar while a request is in flight (shown after 150ms, so fast
   requests show nothing).
3. **Sidebar indicator:** the active item's pill *slides* to the new item (200ms
   `ease-standard`) instead of jumping.
4. **Buttons:** hover lifts the shade (120ms); press scales to 0.98. A loading button
   keeps its width, swaps its label for a spinner plus "Saving…", and morphs to a tick
   for 600ms on success.
5. **KPI tiles:** numbers count up from 0 to their value over 600ms on first view only.
6. **Status changes:** a badge cross-fades its colour and text; an approved row flashes
   a 600ms `success` background wash, then settles.
7. **Skeletons:** a slow shimmer (1.6s) across skeleton rows while loading.
8. **Toasts:** slide in 12px from the edge and fade (200ms); errors gently nudge 4px
   horizontally once.
9. **Modals** fade and scale from 0.96 to 1. **Drawers** slide from the right (320ms).
   **Bottom sheets** slide up and can be dragged down, with a close button as the
   non-drag alternative.
10. **Scanning badge:** a soft breathing pulse (opacity 0.6 → 1, 1.4s) while a file is
    being scanned.
11. **Login and setup pages:** a slow, ambient mesh-gradient background (iris, lime and
    module hues at low opacity, drifting over 30s).

**Reduced motion:** under `prefers-reduced-motion: reduce`, all movement, scaling,
count-ups, shimmer, the ambient gradient and the stagger are removed. Only opacity
fades of 120ms or less remain.

## 7. App shell

**Desktop (≥ 1024px)**
- **Sidebar** (272px, collapsible to a 76px icon rail with tooltips) on `surface`, with a
  hairline right border:
  1. Top: organisation logo and name, and a collapse button.
  2. **Module switcher:** a "Go to…" combobox (Ctrl+K), then the list of modules the
     user can access. Each has a 32px rounded icon tile in its hue (hue at 12% behind
     the icon in full hue).
  3. Under the active module, its sections, with the sliding active pill
     (`iris-tint` background plus a 3px hue bar on the left).
  4. Bottom: Help, Settings (if permitted), and a user mini-card.
- **Top bar** (64px, sticky, glass):
  - left: breadcrumbs (`Expenses / Claims / EXP-0142`);
  - centre: a search field ("Search people, claims, invoices…" with a `Ctrl K` hint);
  - right: Approvals inbox (with a count badge), the Notifications bell (with a lime
    unread dot and a popover panel), a theme toggle, and the profile menu (avatar, name,
    role, My profile, Sessions, Sign out).
- **Page header:** module hue glow behind; title (`h1`) with a status badge; a one-line
  description in `ink-3`; actions on the right (primary iris, secondary outlined); and
  optional tabs underneath with a sliding underline indicator.
- Sticky elements must never hide a focused element (WCAG 2.4.11).

**Mobile (< 768px)**
- Top bar (56px): back chevron on detail pages, or the module name; notifications and
  avatar on the right.
- **Bottom navigation** (64px plus safe area, glass): Home, Approvals (badge), a raised
  centre **"+" New request** button (iris, 56px circle, opens a sheet: Leave, Expense,
  Attendance correction, Ticket), Payslips, More.
- Primary page actions sit in a sticky bottom action bar above the bottom nav.
- Tables become stacked cards. Forms are single-column. Modals become bottom sheets.
  Drawers become full pages.

## 8. Components

Every component has light and dark variants, and these states: default, hover,
focus-visible, active, disabled, loading, error and read-only.

1. **Buttons:** primary (iris), secondary (surface with a `line-strong` border), ghost,
   destructive, icon-only (44px), and split button (primary plus a menu). Labels are
   Inter 500 14px.
2. **Data table:** a card container with a 16px radius. Sticky header (`overline` on
   `surface-sunken`). Sortable columns with `aria-sort` and an animated arrow. Row hover
   tint; selection checkbox column; right-aligned tabular numeric columns. Row action
   menu (kebab, 44px hit area). Pagination with "Rows per page". Column visibility
   menu. A totals footer for money columns.
   - Toolbar: search, filter chips, a **bulk-action bar** that slides down when rows
     are selected ("3 selected · Approve · Export · Clear"), and Export (CSV / Excel).
   - Mobile: stacked cards.
3. **Filter bar:** search plus chips (Status, Date range, Department, Employee) as
   pill buttons that open popovers. Active filters show a count. "Clear all" link.
   Saved views as a segmented control.
4. **Form layout:**
   - Labels above fields, help text in `ink-3` beneath the label, and required fields
     marked with the word "(required)".
   - Inline errors under the field (icon plus text, danger), and an error summary card
     at the top on submit, with links to the fields.
   - Section cards with `h2` titles, and a sticky footer with the primary action right,
     secondary left of it, and Cancel as ghost.
   - Read-only mode renders values as text, not disabled inputs.
5. **Inputs:** text, number, money (₦ prefix, right-aligned, tabular), date picker
   (typing `DD/MM/YYYY` allowed), select, searchable combobox (an employee picker with
   avatar, name and job title), multi-select chips, textarea with a counter, checkbox,
   radio cards, switch, segmented control. Focus: an iris border plus a 4px iris ring
   at 20% opacity.
6. **Status badge:** pill, 12px/600 Inter, with a 6px leading dot, using the status
   colours. Text is always present.
7. **Status stepper:** horizontal on desktop and vertical on mobile. Completed steps
   show an iris tick; the current step has a lime fill with ink text and the word
   "Current"; rejected or returned steps are danger, with the reason. Each step shows
   its actor and date.
8. **Activity timeline:** a vertical line with avatar nodes. Each entry shows actor,
   action, relative time (with the exact time on hover), an optional comment bubble,
   and a field diff (old struck through, new bold). Masked values show "•••• Hidden".
9. **KPI tile:** an `overline` label; a `stat` value; a trend line ("▲ 4.2% vs last
   month", with the arrow and the word, not only colour); a mini sparkline in the
   module hue; an optional lime "New" pill.
10. **Empty state:** a 64px rounded tile in the module hue tint holding a Lucide icon,
    then a title, one sentence and the primary action if permitted. No illustrations.
11. **Toast:** bottom-right on desktop and top on mobile, as a `surface-raised` card
    with a status icon, the message and an optional "Undo" or "View" action. A thin
    progress line shows the time until auto-dismiss (6s). Errors stay until dismissed.
    Announced with `aria-live` (polite; assertive for errors).
12. **File uploader with scan state:** a dashed drop zone with a "Choose file" button
    (dragging is never the only way). It states the accepted types and maximum size.
    Each file becomes a row: type icon, name, size, a progress bar, then a scan badge:
    - "Uploading 45%" (info);
    - "Scanning for viruses" (warning, breathing pulse); the file can't be opened yet;
    - "Ready" (success); the name becomes a link;
    - "Blocked: virus found" (danger), with the line "This file was removed for your
      safety.";
    - "Rejected: file type not allowed" or "Rejected: larger than 10 MB" (danger).

    It also shows the version ("v3 · Show earlier versions") and a remove action if
    permitted.
13. **Confirmation modal:** the title states the action ("Post payroll for October
    2026?"); the body states the consequence ("This creates journal entries. Posted
    payroll can't be edited, only reversed."); the confirm button is named for the
    action. High-risk actions require typing a confirmation word. Focus is trapped; Esc
    cancels.
14. **Drawer** (right, 480 or 640px) for quick view and edit; **bottom sheet** on mobile.
15. **Navigation and feedback:** tabs (sliding underline), breadcrumbs, pagination,
    avatar and avatar stack, tooltip, dropdown menu, popover, skeleton, progress bar,
    and alert banner (info, warning, danger).
16. **Masked value:** e.g. "BVN •••• 5123" with a "Reveal" eye button shown only to
    permitted users; revealing is audited.
17. **"No access" page:** a lock icon in an iris-tint tile; "You don't have access to
    this page"; "If you need it, ask your administrator for access to *Payroll runs*.";
    and a "Back to home" button. It never reveals whether a record exists.
18. **Approval card:** module hue tile, request type, requester (avatar, name,
    department), key figure (amount or dates), "Waiting 3 days", and Approve (primary),
    Reject (secondary; needs a reason) and Open (ghost) actions. Swipe on mobile, always
    with visible buttons as the alternative.
19. **Error pages** (404, 500): centred card on `canvas`, an icon tile, a short
    message, the reference ID on 500, and a "Back to home" button.

## 9. Accessibility: WCAG 2.2 AA

- Contrast: ≥ 4.5:1 for text, and ≥ 3:1 for large text, UI boundaries and focus
  (all tokens pre-checked).
- **44×44px minimum targets.**
- A visible focus ring on every interactive element: 2px `iris` with a 2px `surface`
  offset. It is never removed, and never hidden behind sticky bars (2.4.11).
- Full keyboard operation, logical order, a "Skip to content" link, focus trapped in
  modals and restored on close.
- Visible labels on every input. Errors in text, programmatically associated. Paste
  allowed in password and one-time-code fields (3.3.8). No re-entry of information
  already given (3.3.7).
- A single-pointer alternative for every drag or swipe (2.5.7). Help in the same place
  on every page (3.2.6).
- Colour is never the only carrier of meaning (status, validation, charts, modules).
- Reduced motion respected (section 6). Reflow at 320px, with tables scrolling inside
  their own container and a sticky first column. Charts have a table alternative.

## 10. Instructions for generating screens

- Use only these tokens, components and the shell. Do not invent new colours, fonts,
  radii or shadows. Never use brand colours from other products.
- Render inside the app shell (sidebar and top bar on desktop; top bar and bottom nav on
  mobile). The exceptions are sign-in, MFA, invitation, password reset, first-run setup
  and the public careers page: these use a centred card on the ambient mesh-gradient
  `canvas`, with the Kxmeleon wordmark.
- Apply the active module's hue to the sidebar indicator, its icon tile, the
  page-header glow and charts only.
- Use realistic Nigerian sample data: names such as Adaeze Okafor, Babatunde Adeyemi,
  Fatima Bello, Chinedu Eze and Aisha Mohammed; Lagos, Abuja and Port Harcourt
  branches; ₦ amounts; NIN and BVN masked.
- Every action button represents a permission. For a read-only variant, remove the
  action buttons (don't disable them) and show a small "View only" badge beside the
  title.
