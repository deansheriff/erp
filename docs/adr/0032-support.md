# 0032: Support

Status: proposed (Phase 11e), decisions confirmed 2026-11-26

## Context

The brief (9.12) asks for:

- tickets from staff or customers, with a category, priority and team;
- assignment, and internal versus public comments;
- attachments, SLA targets with breach tracking, and resolution and closure.

Employees see their own tickets, and agents see their team's queue.

## Decisions

**Module.** The work goes in `app/modules/support`. It reads public holidays through
`leave/service.py` and customers through `finance/service.py`.

**Tickets (`TKT-`)** have a subject, description, category, team, priority (Low, Normal,
High, Urgent) and status.

- **Staff tickets:** an employee raises one for themselves.
- **Customer tickets:** a Support Agent records them for a finance customer, with a
  contact email (by default, the customer's).
- **No customer logins:** public replies and resolutions are emailed to the requester (the
  employee, or the customer's contact).

**Teams and categories.**

- Administrators set up support teams with a lead and agents.
- Each category routes tickets to one team.
- Agents see their own teams' queues.

**Assignment.**

- New tickets land unassigned in the team's queue.
- An agent on the team takes a ticket.
- The team lead assigns or reassigns tickets to the team's agents.

**Comments and attachments.**

- Public replies are seen by the requester; internal notes only by agents.
- Files attach to the ticket (file purpose `support_attachment`). Requesters see them, and
  so do agents of the team.

| Status | When |
|---|---|
| Open | Raised, nobody working it yet. |
| In progress | An agent is working it. A requester's reply also moves it back here. |
| Waiting on requester | Pauses the resolution clock. |
| Resolved | Needs a resolution note. The requester can reopen it, or rate it from 1 to 5. |
| Closed | Automatic, 3 business days after resolution (editable, to confirm). It can't be reopened; a new ticket is raised instead. |

**SLA targets** are counted in business hours: Monday to Friday, 08:00 to 17:00 Lagos
time, skipping public holidays from Leave. All of these defaults are to confirm:

| Priority | First response | Resolution |
|---|---|---|
| Urgent | 1 hour | 4 hours |
| High | 4 hours | 1 day (9 hours) |
| Normal | 1 day | 3 days |
| Low | 2 days | 5 days |

How the clocks run:

- **First response:** met by the first public reply from an agent.
- **Resolution:** runs from creation to resolution. Time spent waiting on the requester,
  or resolved before a reopen, doesn't count.
- **Breaches:** a ticket is marked breached when a target passes.

**Alerts.**

- At 75% of the resolution target (editable), the assignee is warned once, or the team lead
  if nobody is assigned.
- On a breach, the team lead is emailed once.

**SLA report.** Met and breached counts by team, priority and month, average resolution
time, and the average rating by team and agent.

**Permissions.**

| Permission | Who has it |
|---|---|
| `support.ticket.create` | Employees, for themselves |
| `support.ticket.read` | Employees for their own tickets; Auditors for all |
| `support.ticket.work` | Support Agents, on their teams' tickets and customer tickets |
| `support.report.read` | Support Agents and Auditors |
| `support.settings.manage` | Organisation administrators (teams, categories, SLA, business hours) |

## Consequences

- A background task (every 15 minutes) updates breaches, sends the warnings and alerts,
  and closes resolved tickets.
- The screens were requested from Stitch and are marked `stitch_pending` until the
  designs arrive.
