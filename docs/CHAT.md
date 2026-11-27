# Chat

Phase 12 (ADR 0034). Permissions are in `docs/PERMISSIONS.md`.

## Tables

| Table | What it holds |
|---|---|
| `chat_conversations` | Direct messages (one per pair, by `direct_key`), groups (with a name) and department channels (one per department, read-only once it's deactivated). |
| `chat_members` | Current members, their role (member or admin) and their last-read message. Removing someone deletes the row. |
| `chat_messages` | Text up to 4,000 characters, when it was edited, and who deleted it and when. Ids are UUIDv7, so they sort by time. |
| `chat_mentions`, `chat_reactions`, `chat_pins` | Who's mentioned in a message; reactions from a fixed set (👍 ❤️ 😂 🎉 😮 🙏); pins with who pinned them. |
| `chat_attachments` | Files on a message (file purpose `chat_attachment`). |
| `chat_settings`, `chat_email_notices` | The edit window, the email delay and retention; the unread emails already sent. |

## Who can do what

**Anyone with chat (every employee):**

- message any colleague who can use chat, start groups, and take part in the
  conversations they belong to;
- react to and pin any message;
- edit their own messages for 15 minutes, and delete them at any time.

**Group admins:**

- the creator, and anyone they promote;
- add, remove and promote members, rename the group, and delete any message in it.

**Channel moderators:**

- the current holders of the department's top positions, and anyone with
  `chat.channel.moderate` (Organisation Administrators);
- delete any message in the channel.

**Organisation Administrators** also change the chat settings.

Membership is the access rule. Someone who isn't a current member gets "not found" for
the conversation, its messages and its attachments. That includes administrators.

## Department channels

Every active department has a channel. Its members are everyone with an account who
holds a position (primary, acting or interim) in the department or one below it.

- **Syncing:** a task runs every 10 minutes, and each person's own channels sync when
  they open chat. Joining or leaving a department moves people in and out.
- **Closed departments:** a deactivated department's channel keeps its history but
  turns read-only.

## The page

- **The list:** channels, groups and direct messages, with unread counts. The open
  conversation shows a "New" line at the first unread message.
- **Reading:** messages from the same person within ten minutes are grouped. Mentions of
  you are highlighted.
- **Under the latest message:** "Seen by" names in direct messages and groups, or a count
  in channels.
- **Sending:** Enter sends and Shift+Enter makes a new line. A file can go with the
  message.
- **Each message's menu:** react, pin or unpin, and delete. Your own recent messages can
  be edited.

## Real-time updates

The page opens a WebSocket to `/ws/chat`.

- **Notices only:** the socket carries small notices (a new message, a change, read,
  membership), never message content. The page then fetches what changed through the
  ordinary routes, which check membership.
- **Fan-out:** notices go to each member's own channel once the change commits. Redis
  carries them across processes (`KX_REALTIME_BACKEND=redis`, required in production).
  `memory` keeps them inside one process, for tests and development without Redis.
- **Checks:** the socket refuses another site's Origin. It needs a live session cookie
  with MFA satisfied, and re-checks the session every minute without counting it as
  activity.
- **Revocation:** signing out, revoking a session, a password change or reset,
  suspension or exit closes the user's sockets straight away. The page then reloads to
  the sign-in screen.
- **Without a socket:** sending still works as a form post, and the list refreshes on
  navigation.

## Email

Every 5 minutes, someone with a mention, or a direct message, still unread after 30
minutes gets an email. It names the conversation and the sender, never includes the
message, and goes at most once per conversation per day.

## Defaults (confirm before go-live)

- Edits are allowed for 15 minutes after sending.
- Unread emails go after 30 minutes.
- Messages and attachments are kept indefinitely. When a retention period is set (at
  least 30 days), a nightly task deletes older messages and their files.
- Deleted attachments are removed from storage after 30 days, unless on legal hold.
