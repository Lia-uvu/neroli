# Neroli normalized adapter contract v2

This is the public boundary between a source adapter and Neroli. The adapter
describes immutable source events; Neroli owns canonical IDs, conversational
rounds, local privacy routing, persistence, and downstream wakeups.

This is the implemented compatibility contract for linear adapters. The schema
v12 tree contract moves room origin to the adapter, accepts incremental nodes,
and stores optional rendering observations without main/active-branch semantics;
see the
[conversation tree adapter contract](conversation-tree-adapter-contract.md).

For the responsibility split, current tree semantics, and explicitly unsupported
deletion operations, read the [source adapter boundary](source-adapter-boundary.md).

## Envelope

One JSON file carries one source and one source route. It may contain messages
from more than one native session.

This envelope is a replayable delivery batch, not a nested tree object and not an
authoritative replacement snapshot. Parent fields carry tree-shaped provenance;
absence from a later envelope does not delete previously ingested occurrences.

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

For the current implementation, every native session touched by an updated
delivery must include its complete known ordered trajectory. Delta-only delivery
is not yet supported because Neroli derives rounds from the messages present in
the delivery.

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

`native_parent_session_id` must be a stable native session ID, not a source file
path. `native_parent_message_id` must identify a source message parent, not an
omitted harness-only event. If the adapter cannot establish either relationship
without guessing, it omits the field.

Because parent-session provenance is session-level source fact, messages carrying
the same `native_session_id` should consistently carry the same
`native_parent_session_id`, or consistently omit it.

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
- When a known session grows, replay its complete known trajectory with the same
  native IDs and source sequence values plus the new nodes.
- Missing nodes are not deletions. V2 currently has no tombstone, branch
  replacement, or source-deletion operation.

## Legacy compatibility

Older normalized JSON arrays remain accepted. Their `session_id`, `round`, and room
behavior are preserved exactly for existing queues and archives. They do not gain the
strict v2 namespace, immutable-conflict check, or explicit source-route policy. New
adapters should emit only v2 envelopes.
