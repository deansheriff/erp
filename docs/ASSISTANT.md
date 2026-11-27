# AI assistant

Phase 13 (ADR 0035). Permissions are in `docs/PERMISSIONS.md`.

The assistant only reads. It drafts and answers; people decide. Nothing goes to an AI
provider until a Super Administrator turns it on and accepts what may leave the system.

## Turning it on

In **Assistant → Settings** (Super Administrators only):

1. **Provider:** Anthropic (Claude), OpenAI or Google Gemini. Then the model id: for
   Anthropic, `claude-opus-5-5`; for OpenAI and Gemini, a model from your account.
2. **API key:** it's encrypted when saved and never shown again, only its last four
   characters.
3. **What may leave the system:** tick the kinds of data the provider may receive.

   | Kind | What's sent | Needed by |
   |---|---|---|
   | Policies | the text of the policy library | policy questions |
   | Directory | names, job titles, departments, grades | job descriptions, appraisal evidence |
   | Work records | project hours and task counts, tickets resolved and their ratings, days clocked in and late, leave days (never the leave type) | appraisal evidence |
   | Support tickets | a ticket's subject, description and public replies, and category names | ticket suggestions |

   Personal, pay, bank, statutory and sensitive fields are never sent.
4. **Limits:** requests per person each month (200) and days of history kept (90).
5. **Turn on:** this needs a tick accepting that the chosen data goes to the provider.
   The acceptance records who gave it and when, and is audited.
6. **Changing the provider or the data** turns the assistant off until someone accepts
   again.
7. **Test connection** sends a tiny request with no organisation data.

## Features

| Feature | Where | Who |
|---|---|---|
| Ask about policies | Assistant → Ask about policies | every employee |
| Job description and interview questions | Assistant → Job descriptions | HR Managers and HR Officers |
| Appraisal evidence | Assistant → Appraisal evidence | line managers for their team; HR Managers for everyone |
| Ticket category and reply | the Assistant panel on a support ticket | support agents on the ticket's team |

- **Policy questions** are answered only from the policy library. When no passage
  matches, nothing is sent.
- **Job descriptions** are drafted from the position's details in the org chart (title,
  department, grade, location, reporting line) and your notes. They never include pay,
  or requirements about age, sex, religion, ethnicity, state of origin, marital status
  or disability.
- **Appraisal evidence:**
  - It lists the recorded facts beside a neutral summary, and never rates.
  - A summary that uses rating language ("meets expectations", "performance rating",
    "out of 10") is withheld.
  - When nothing is recorded, nothing is sent.
- **Ticket suggestions:**
  - The category is advice: support has no way to change a ticket's category yet.
  - The draft reply opens in a reply box for the agent to edit and send.
  - Internal notes are never sent.

## Checked quotes

Every answer cites its sources with exact quotes, and each quote is checked against the
source text.

- **A quote that doesn't match** is removed, and the page says so.
- **A policy answer or evidence summary left with no checked quote** is marked as not
  checked.
- **Anything in a source that looks like an instruction** is treated as data, not
  followed.

## The policy library

HR adds policies as PDF, Word or text files, with a version and the date they take
effect.

- **Searching:** the text is read at upload and split into passages. Only each policy's
  current version is searched; earlier versions stay readable.
- **Scanned PDFs** without a text layer can't be used.
- **Readers:** every signed-in person can read the policies.

## History, limits and audit

- **History:** each person sees their own questions and drafts under Assistant →
  History. They're deleted after 90 days.
- **Cap:** a person may make 200 requests a month. Failed calls to the provider don't
  count. Usage by person and feature is on the settings page.
- **Audit:** every request is audited with the feature, provider, model, the kinds of
  data sent and how many sources were used, never the text.

## Defaults (confirm before go-live)

- Off, with nothing accepted.
- 200 requests per person a month.
- History kept 90 days.
- Policy files kept six years after a version is replaced.
