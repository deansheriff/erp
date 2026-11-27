# Projects

Phase 11d (ADR 0031). Revenue and costs live in the ledger: journal lines can name a
project. Permissions are in `docs/PERMISSIONS.md`.

## Tables

| Table | What it holds |
|---|---|
| `projects` | Code (`PRJ-`), name, client (a customer), manager, dates, cost centre, description and status. |
| `project_budgets` | The budget by category: labour, materials, expenses, other. |
| `project_members` | Who works on it (the manager is always on the team). |
| `project_milestones`, `project_tasks` | Milestones with due dates; tasks with a milestone, assignee, due date and status. |
| `timesheets`, `time_entries` | One sheet per person, project and week (from Monday), with hours by day and task. |
| `project_cost_rates` | Each person's hourly cost, effective-dated, for labour cost only. |
| `project_settings`, `project_budget_alerts` | Alert levels, and the levels already emailed for each project. |

A `project_id` column is also added to:

- journal lines;
- sales, purchase, bill and expense lines;
- expense journal lines;
- stock documents.

## Statuses

| Status | Meaning |
|---|---|
| Planned | Set up, not started. |
| Active | Time and costs can be recorded. Only active projects appear in the project pickers. |
| On hold | Paused. |
| Completed, Cancelled | Closed. Submitted timesheets must be decided first. |

## Who does what

- **Project Manager** (new role): sets up projects and manages the ones they lead,
  including the members, budget, milestones, tasks and status.
- **Members**: see their projects, log time, and move tasks assigned to them.
- **Finance Managers, Directors and Auditors**: see every project with its figures. A
  manager sees the figures of their own projects.
- **Finance Managers**: set hourly cost rates. Finance Managers and Directors set the alert
  levels.

## Time

1. Members enter hours in quarters, by day, on the week's grid. A task is optional.
2. Each project's week is submitted separately to the project's manager.
   - If the manager logged the time themselves, it goes to their line manager.
   - With neither, it goes to someone who can step in (Directors).
3. A rejection needs a reason, and the week can then be corrected and sent again.
4. Approved hours times the person's rate on each day give the labour cost. It isn't
   posted, because payroll already posts salaries.

## Profitability

| Figure | From |
|---|---|
| Revenue | Posted income lines naming the project (invoices, credit notes, journals). |
| Materials | Stock issues, bills, and the cost of stock sold on the project's invoices. |
| Expenses | Expense claims. |
| Other | Anything else, such as manual journals. |
| Labour | Approved hours at the cost rates. |

A reversal counts under its original journal's category.

**Budget alerts:** a daily task (08:15 in Lagos) emails the manager once when a project's
total cost passes each alert level of its total budget.

## Defaults (confirm before go-live)

- Budget alerts at 80% and 100%.

## Screens

Projects → Projects (each project, its form and tasks), My timesheet, Time approvals,
Profitability, Settings (alerts and cost rates). Journals, invoices, purchase orders,
direct bill lines, expense claim lines and stock issues gain a project picker.
