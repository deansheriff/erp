# 0031: Projects

Status: proposed (Phase 11d), decisions confirmed 2026-11-26

## Context

The brief (9.11) asks for:

- projects with a client, budget and dates;
- milestones;
- tasks with assignees, status and due dates;
- time entries with approval;
- expenses and costs linked to projects;
- project profitability.

Expenses (ADR 0020) left a project field for this phase.

## Decisions

**Module.** The work goes in `app/modules/projects`.

- **Finance's side:** a project is an optional dimension on journal lines, like a cost
  centre. Finance never imports projects. It asks for project choices, and whether one is
  open, through `finance/project_link.py`. Projects implements that link, and
  `app/wiring.py` connects the two, as for stock (ADR 0029).
- **Other modules:** expenses and inventory use the same link through `finance/service.py`.

**Projects (`PRJ-`)** have:

- a name, an optional client (a finance customer), a manager (an employee), start and end
  dates, an optional cost centre and a description;
- members;
- a budget by category: Labour, Materials, Expenses and Other.

| Status | Meaning |
|---|---|
| Planned | Set up, not started. |
| Active | Time and costs can be recorded against it. |
| On hold | Paused. |
| Completed | Closed. Needs all its submitted time decided. |
| Cancelled | Closed. Needs all its submitted time decided. |

**Who can do what.**

- A new Project Manager role creates projects and manages the ones they lead.
- Members see their projects and tasks.
- Finance Managers, Directors and Auditors see every project, with its figures. A manager
  sees the figures for their own projects.

**Milestones and tasks.**

- Milestones have a due date and are marked done.
- A task optionally belongs to a milestone and has one assignee from the members.
- A task's status is To do, In progress, Blocked or Done, and it has a due date.

**Time.**

- **Timesheets:** a member logs hours by day against an active project, and optionally a
  task. Each person has one weekly timesheet per project, starting Monday.
- **Approval:** submitting sends the week to the project's manager. If the manager logged
  the time themselves, their line manager decides it instead. With neither, someone with
  "step in on time" decides it (the Director role).
- **Rejection:** needs a reason, and the week goes back to draft.

**Labour cost.**

- Finance keeps an hourly cost rate per employee, effective-dated.
- Labour cost is approved hours times the rate in force on each day.
- It appears in profitability only. It isn't posted, because payroll already posts
  salaries.
- Hours with no rate count as zero, and the screens say so.

**Linking costs and revenue.** These lines can name an active project:

- invoice and credit note lines;
- purchase order lines, which carry it to their bills;
- direct bill lines;
- expense claim lines;
- stock issues;
- manual journal lines.

The project travels with each line into its journal.

**Profitability** is read from the ledger:

- **Revenue:** postings to income accounts that name the project.
- **Costs:** postings to expense accounts that name the project, put into budget
  categories by where the journal came from:

| Journal source | Budget category |
|---|---|
| Stock issues | Materials |
| Bills (payables) | Materials |
| Cost of stock sold on the project's invoices | Materials |
| Expense claims | Expenses |
| Anything else (e.g. manual journals) | Other |
| Approved time | Labour (not posted) |

**Budget alerts.** A daily task emails the manager once when a project's total cost
(labour included) passes each threshold: 80% and 100% of the total budget. Both
thresholds are editable, to confirm.

**Billing.** Revenue comes from invoices that name the project. Time isn't invoiced from
the project in this phase.

## Consequences

- Journal lines, journals' system lines, and document lines (sales, purchase, bill,
  expense) gain an optional `project_id`, as do stock documents.
- Lines are merged by account, cost centre and project.
- The screens were requested from Stitch and built from the design system meanwhile;
  their `screens.yaml` entries are marked `stitch_pending` until the designs arrive.
- Recurring journal templates don't name projects.
