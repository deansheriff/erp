"""Chat permissions (ADR 0034). Using chat is for every employee; which conversations
someone can reach is decided by membership, checked on every action."""

from app.authz.permissions import ORG_ONLY, Permission

# Held for oneself: which conversations someone reaches is decided by membership.
USE = Permission(
    "chat.conversation.use",
    "Use chat",
    "Send direct messages, start groups and take part in the conversations you belong to.",
)
MODERATE = Permission(
    "chat.channel.moderate",
    "Moderate department channels",
    "Delete any message in a department channel.",
    scopes=ORG_ONLY,
)
SETTINGS = Permission(
    "chat.settings.manage",
    "Manage chat settings",
    "Change the edit window, the unread email delay and how long messages are kept.",
    scopes=ORG_ONLY,
)

PERMISSIONS = (USE, MODERATE, SETTINGS)
SOD_RULES = ()
