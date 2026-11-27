# 0035. The AI assistant: read-only, opt-in, cited

- Status: Accepted
- Date: 2026-11-27

## Context

Phase 13 builds the assistant from brief sections 8 and 9.16. The brief's rules:

- it is read-only and never takes actions;
- it retrieves only records the actor's scope allows;
- it cites its sources and checks every cited quote against the source text;
- it is off by default and sends nothing to an external model until an administrator
  opts in and accepts which data classes may leave the system.

Its features:

- answer questions from the company's policies, with citations;
- draft job descriptions and interview questions;
- summarise recorded evidence for an appraisal, never rating;
- suggest a ticket's category and a reply draft.

Everything is a draft for a person to accept.

The product owner settled the business rules on 2026-11-27:

- **Scope:** the brief's assistant only; the roadmap items stay for later.
- **Provider:** the administrator chooses Anthropic, OpenAI or Gemini.
- **Policies:** a new policy library.
- **Appraisal evidence:** taken from records that already exist.
- **Who uses it:** each feature by role.
- **History:** kept 90 days, visible to its owner only.
- **Limit:** 200 requests per person per month.

## Decisions

**Module.** The work goes in `app/modules/assistant`. The provider adapters sit in
`app/modules/assistant/providers/`.

**Off until an administrator opts in** (`system.ai.manage`, Super Administrators only).
The settings hold:

- whether the assistant is on;
- the provider and model;
- the API key, encrypted at rest (ADR 0011) and never shown again;
- the **kinds of data accepted to leave**;
- the monthly cap and the history period.

Turning the assistant on needs an explicit acceptance naming the provider and the kinds
of data. The acceptance is recorded with who gave it and when, and audited. Changing the
provider or the kinds of data needs a fresh acceptance.

**Kinds of data that may leave.** Each feature needs certain kinds, and it stays
unavailable until those kinds are accepted.

| Kind | What it is | Used by |
|---|---|---|
| Policies | the text of the policy library | policy questions |
| Directory | names, job titles, departments, grades | job descriptions, appraisal evidence |
| Work records | hours on projects, task counts, tickets resolved and their ratings, days clocked in and late, days on leave (never the leave type) | appraisal evidence |
| Support tickets | a ticket's subject, description and public replies, and the category names | ticket suggestions |

Personal, compensation, banking, statutory and sensitive fields are never sent in this
phase, whatever is accepted.

**Providers.** One interface, `generate(system, prompt, schema) -> Reply`, with one
adapter per provider. Each asks for JSON that matches a schema.

| Provider | How it's called | Default model |
|---|---|---|
| Anthropic | the official `anthropic` SDK; structured output (`output_config.format`); the server-side refusal fallback on the models that take it; refusals handled | `claude-opus-5-5` |
| OpenAI | the Chat Completions REST API with a strict `json_schema` response format | none: the administrator enters one |
| Gemini | the `generateContent` REST API with a JSON response schema | none: the administrator enters one |

- **Limits:** every call has a 120-second timeout and `max_tokens` 16,000.
- **Tests:** they use a fake provider. No test reaches the internet.

**Sources, citations and checking.**

- **Numbered sources:** every feature hands the model numbered sources, which it must
  treat as data, never as instructions. These are passages from policies, facts about a
  person, a position's details, or a ticket's text.
- **Citations:** the model answers with text plus citations, each naming a source and
  quoting it exactly.
- **Checking:**
  - The server checks each quote against its source, ignoring case, whitespace and
    curly-versus-straight quotes and dashes.
  - A quote that doesn't match is removed, and the page says so.
  - A policy answer left with no checked citation is marked "couldn't be checked against
    the policies".
- **No sources, no call:** when nothing relevant is found, the assistant says so without
  calling the model.

**Policy library.**

- **Who manages it:** HR (`assistant.policy.manage`) uploads policies (PDF, Word or text)
  through the files service (purpose `assistant_policy`), with a title, version and
  effective date. Every signed-in person can read them.
- **Text:** extracted at upload and split into passages of about 900 characters.
- **Finding passages:** Postgres full-text search, any-word match ranked by relevance,
  top 6 passages.
- **Versions:** only each policy's current version is searched. Retired versions stay
  readable.

**Features and who uses them.**

| Feature | Permission | Granted to |
|---|---|---|
| Policy questions | `assistant.policy.ask` | Employee (for themselves), which keeps that role self-service only |
| Job descriptions and interview questions | `assistant.hiring.draft` | HR Manager, HR Officer |
| Appraisal evidence | `assistant.evidence.summarise` | Line Manager (team), HR Manager (organisation) |
| Ticket suggestions | `assistant.ticket.suggest` | Support Agent |
| The policy library | `assistant.policy.manage` | HR Manager, HR Officer |
| Settings and usage | `system.ai.manage` | Super Administrator |

- **Appraisal evidence:**
  - It is checked against the person with the permission's scope, so a line manager
    reaches only their team.
  - The facts come from each module's `service.py`: projects, support, attendance and
    leave. The evidence permission is what allows reading them for this purpose.
  - The prompt forbids ratings, and the reply is refused if it contains rating language
    (a performance rating, "meets/exceeds/below expectations", "out of 10").
- **Ticket suggestions:**
  - They appear on the support ticket page for agents.
  - The suggested category must be one of the existing categories.
  - The reply draft lands in the agent's reply box, to edit and send themselves.

**History, usage and audit.**

- **History:** each request is stored with its inputs, the reply and its checked
  citations. Only its owner can see it, and it's deleted after 90 days by a nightly task
  (editable, to confirm).
- **Cap:** a person may make 200 requests a calendar month (editable, to confirm).
  Administrators see usage by person and feature.
- **Audit:** every request records the feature, provider, model, the kinds of data sent
  and how many sources were used, never the text.

## Consequences

- New runtime dependencies: `anthropic` (the official SDK), `httpx` (OpenAI and Gemini
  over REST) and `pypdf` (policy text).
- A policy is read at upload, before the virus scan finishes. Text extraction is
  pure-Python, and the file itself stays unreadable until it's clean.
- Scanned PDFs with no text layer can't be used. The upload is refused with that reason.
- The screens were requested from Stitch and are marked `stitch_pending` until they
  arrive.
