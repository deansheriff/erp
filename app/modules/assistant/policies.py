"""Assistant permissions (ADR 0035). Each feature is its own permission, granted by role.
The settings, which decide whether anything may leave the system, are `system.ai.manage`
(Super Administrators only)."""

from app.authz.permissions import ORG_ONLY, Permission, Scope

ASK = Permission(
    "assistant.policy.ask",
    "Ask about policies",
    "Ask the assistant questions answered from the policy library, with checked quotes.",
)
MANAGE_POLICIES = Permission(
    "assistant.policy.manage",
    "Manage the policy library",
    "Add policies and new versions of them for the assistant to answer from.",
    scopes=ORG_ONLY,
)
HIRING = Permission(
    "assistant.hiring.draft",
    "Draft job descriptions",
    "Have the assistant draft a job description and interview questions for a position.",
    scopes=ORG_ONLY,
)
EVIDENCE = Permission(
    "assistant.evidence.summarise",
    "Summarise appraisal evidence",
    "Have the assistant summarise what's recorded for a person over a period. It never rates.",
    scopes=(Scope.TEAM, Scope.DEPARTMENT, Scope.ORGANIZATION),
)
TICKETS = Permission(
    "assistant.ticket.suggest",
    "Suggest ticket categories and replies",
    "Have the assistant suggest a support ticket's category and draft a reply.",
    scopes=ORG_ONLY,
)
SETTINGS = Permission(
    "system.ai.manage",
    "Manage the AI assistant",
    "Turn the assistant on or off, choose the provider and model, and accept which data "
    "may be sent to it.",
    sensitive=True,
    scopes=ORG_ONLY,
)

PERMISSIONS = (ASK, MANAGE_POLICIES, HIRING, EVIDENCE, TICKETS, SETTINGS)
SOD_RULES = ()
