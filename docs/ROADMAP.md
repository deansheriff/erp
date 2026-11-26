# Roadmap

Work agreed for later, beyond the brief's phases. Nothing here is built yet. Each item
gets its own design (ADR) and screens when it's scheduled.

## AI features (agreed 2026-11-26)

These rules apply to every AI feature:

- **Access:** agents act through the same services and permissions as people, with their
  own user account. Every action is in the audit log.
- **Confirmation:** anything that changes data is drafted for a person to confirm.
  Separation-of-duties rules still apply, so an agent never approves its own work.
- **Privacy:** sensitive fields (pay, bank details, statutory IDs) are only shown to an
  agent acting for someone allowed to see them.
- **Statutory figures:** they stay as confirmed configuration; AI only flags them.

### AI workforce

- Agents that carry out tasks given in plain language, for example:
  - "create a new employee using these details";
  - "generate a response to this email".
- A task inbox to assign work to an agent and see its drafts, questions and results.

### Documents in, records out

- **Supplier bills:** a PDF or photo becomes a draft bill, with supplier, VAT, WHT and a
  match to the purchase order.
- **Expense receipts:** the date, amount and category are read, and duplicates flagged.
- **Bank statements:** a statement is read into the import, with suggested matching rules.
- **CVs and offer letters:** they become new-employee drafts.
- **Supplier quotes:** a quote PDF becomes the RFQ's quote form.

### Drafting

- Email replies to customers and suppliers, with context from their account.
- Chasing letters for overdue invoices, in a chosen tone.
- Job descriptions, offer letters and warning letters from HR templates.
- RFQ wording, and the reason for awarding a quote that isn't the lowest.
- Plain-language payslip explanations for staff.

### Answering questions

- Questions in plain English across modules ("What did we spend on diesel last
  quarter?"). Answers respect the person's permissions and link to the records behind
  them.
- An explainer for any figure: why profit fell, why a bill was held, why stock is low.

### Checks and warnings

- **Unusual entries:** duplicate bills, round-sum expenses, changes to supplier bank
  details just before a payment, journals posted at odd hours.
- **Stock:** forecasts of when items run out, and suggested reorder quantities.
- **Cash:** a cash flow forecast from receivables, payables, payroll and recurring
  journals.
- **Projects:** warnings when a project is heading over budget or late.
- **Customers:** predictions of late payment, and suggested credit limits.

### Nigerian compliance

- Checks on PAYE, pension, NHF, VAT and WHT settings against the confirmed statutory
  figures, flagging ones that look out of date.
- Draft filing summaries for VAT, WHT and PAYE returns, ready for review.

### HR

- Leave and expense requests sorted for approvers, with a suggested decision and reason.
- Draft performance review summaries from goals and feedback.
- Answers to staff questions from the organisation's policies.

### Support and projects

- Support tickets classified and routed, with suggested replies and SLA risk warnings
  (after Phase 11e).
- A project brief broken into milestones and tasks, and weekly status reports drafted.

### Everyday use

- Meeting and voice notes turned into tasks, leave requests or expense lines.
- A daily briefing for each person: approvals waiting, things due, and what's unusual.
- Plain-language setup help ("set up a pay group for weekly casual staff").
