# Support

Phase 11e (ADR 0032). Permissions are in `docs/PERMISSIONS.md`.

## Tables

| Table | What it holds |
|---|---|
| `support_teams`, `support_team_agents` | Teams with a lead and agents. |
| `support_categories` | What tickets are about; each routes to one team. |
| `support_tickets` | Tickets (`TKT-`). Each has its requester (an employee) or customer and contact email, the assignee, priority and status, the SLA due times and paused minutes, breach flags, the resolution and the rating. |
| `support_comments` | Public replies, internal notes and system lines (taken, assigned, reopened, closed). |
| `support_attachments` | Files on a ticket (file purpose `support_attachment`). |
| `support_sla_targets`, `support_settings` | Targets by priority in business minutes; business hours, the warning level and when resolved tickets close. |

## How a ticket moves

1. **Raised.**
   - An employee raises one for themself.
   - A Support Agent can record one for a finance customer. The contact email defaults to
     the customer's.
   - The category decides the team, and the ticket lands unassigned in that team's queue.
2. **Taken or assigned.** An agent on the team takes it, or the team lead assigns it. It
   becomes In progress.
3. **Replies.**
   - Public replies are emailed to the requester, or to the customer's contact.
   - Internal notes are for agents only.
   - "Send and wait on requester" pauses the resolution clock until they reply, or an agent
     puts it back in progress.
4. **Resolved.** Resolving needs a note, which is emailed to the requester. The requester
   can rate it from 1 to 5, or reopen it.
5. **Closed.** It closes automatically after 3 business days, and can't be reopened.

## SLA

Targets are business hours: Monday to Friday, 08:00 to 17:00 Lagos time, skipping public
holidays from Leave.

- **First response** is met by the first public reply from an agent.
- **Resolution** runs from creation to resolution, leaving out time spent waiting on the
  requester (or resolved before a reopen).

Every 15 minutes, a task:

- marks breaches;
- warns the assignee (or the lead, if nobody is assigned) once at 75% of the resolution
  target;
- emails the team lead once on a resolution breach;
- closes resolved tickets.

The SLA report shows tickets resolved in a month, by team and priority and by agent:
response and resolution met, breaches, average resolution time and average rating.

## Defaults (confirm before go-live)

| Priority | First response | Resolution |
|---|---|---|
| Urgent | 1 hour | 4 hours |
| High | 4 hours | 9 hours (1 day) |
| Normal | 9 hours (1 day) | 27 hours (3 days) |
| Low | 18 hours (2 days) | 45 hours (5 days) |

- **Business hours:** 08:00 to 17:00.
- **Warning:** at 75% of the resolution target.
- **Closing:** 3 business days after resolution.
- **Attachments:** kept for two years.

## Screens

Support → Tickets (each ticket, and raising one), SLA report, Settings (teams,
categories, SLA targets and hours).
