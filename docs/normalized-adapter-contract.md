# Neroli normalized adapter contract v2

This is the public boundary between a source adapter and Neroli. The adapter
describes immutable source events; Neroli owns canonical IDs, conversational
rounds, local privacy routing, persistence, and downstream wakeups.

## Envelope

One JSON file carries one source and one source route. It may contain messages
from more than one native session.

```json
{
  "format": "neroli-normalized-v2",
  "source": "example-adapter",
  "source_route": "resident-entry",
  "messages": [
    {
      "native_session_id": "session-123",
      "native_parent_session_id": "session-122",
      "native_message_id": "message-17",
      "native_parent_message_id": "message-16",
      "role": "assistant",
      "text": "A source message, unchanged.",
      "occurred_at": "2026-07-31T12:00:00Z",
      "source_sequence": 17,
      "provider": "provider-name",
      "model": "provider/model-name"
    }
  ]
}
```

Envelope fields:

- `format` must be `neroli-normalized-v2`.
- `source` is the stable adapter/source namespace. It is not a model name or a room.
- `source_route` is a stable source-side route label. It does not grant room access.
  The receiving instance must map `(source, source_route)` to a configured room in
  its private `settings.json`.
- `messages` is an array. Invalid v2 records fail the whole ingest instead of being
  silently reinterpreted.

Message fields:

- `native_session_id`, `native_message_id`, `role`, `text`, `occurred_at`, and
  `source_sequence` are required.
- `native_parent_session_id`, `native_parent_message_id`, `provider`, and `model`
  are optional.
- `role` is exactly `user` or `assistant`.
- `occurred_at` is ISO 8601 with a UTC offset.
- `source_sequence` is a non-negative integer, unique within a native session. It
  is source order, not a Neroli round number.

## Identity and ordering

Neroli canonicalizes identity independently of spool paths:

```text
session_id  = "session:" + sha256(source + "|" + native_session_id)[:32]
source_uuid = "message:" + sha256(source + "|" + native_message_id)[:32]
```

`native_message_id` therefore must be globally stable inside one `source`. If a
platform only provides session-local message IDs, its adapter must pre-scope them,
for example `native_session_id + ":" + local_message_id`.

Copied history keeps the same native message IDs across sessions, so one canonical
message can appear in multiple `turns` rows and fork overlap remains visible. A real
edit is a new immutable source event and must receive a new native message ID. Reusing
an existing ID with changed role, text, time, or parent is a hard error.

Neroli sorts each canonical session by `source_sequence`. Every `user` event starts
a new `round`; following assistant events receive increasing `message_seq`. Adapters
do not submit Neroli round numbers.

## Local room policy

Room ownership is private instance policy, not portable adapter data:

```json
{
  "ingest": {
    "source_routes": {
      "example-adapter": {
        "resident-entry": "main"
      }
    }
  }
}
```

The v2 loader rejects missing or unknown routes and routes mapped to a room absent
from `rooms.json`. It never falls through to the default room. The resolved result is
stored in `source_sessions`; cards for that session use it before any legacy
source-file inference.

## Persistence and retry

- `source_sessions` stores canonical/native session provenance, parent session,
  source route, and the locally resolved room.
- `messages` stores immutable canonical content plus native message provenance.
- `turns` stores each occurrence and derived ordering, unique on
  `(session_id, source_uuid)`.
- Replaying the same export is idempotent. Real `turns` changes advance the shared
  watermark and wake the source-independent Card Gen watcher.

## Legacy compatibility

Older normalized JSON arrays remain accepted. Their `session_id`, `round`, and room
behavior are preserved exactly for existing queues and archives. They do not gain the
strict v2 namespace, immutable-conflict check, or explicit source-route policy. New
adapters should emit only v2 envelopes.
