# Conversation tree adapter contract

Status: **tree/observation contract implemented in v12; overlapping Card membership restored in v13**

Neroli has two layers:

```text
source-neutral conversation tree
  -> Card generation and other derived memory views
```

The first layer stores the simplest faithful structure it can: a forest of nodes
and parent edges. A room may contain many disconnected conversation roots. It does
not decide which branch is main, active, continued, withdrawn, preferred, or
abandoned. The Card layer applies the same policy to every captured branch. Shared
ancestors are context and are not carded again merely because another branch
reuses them.

[`neroli-normalized-v2`](normalized-adapter-contract.md) remains a compatibility
format for adapters that still submit complete linear session trajectories. New
tree-aware adapters use this incremental contract.

## Source format comparison

| Concern | Pi 0.83.0 | Claude Code JSONL | Tree contract |
|---|---|---|---|
| Container | One header plus append-only entries in a JSONL session file | Repeated JSONL records carrying `sessionId`; one logical session may span files | Adapter-private provenance; conversation components are defined by roots and parent edges |
| Node identity | Stable entry `id` | Top-level `uuid`, reused by copied/replayed history | Stable `native_node_id`, namespaced by source and room |
| Parent edge | Entry `parentId` | Top-level `parentUuid`; compaction may also expose `logicalParentUuid` | One optional `native_parent_node_id` per normalized node |
| Branches | Sibling entries coexist in one file; fork/clone may create another file | Shared UUIDs, copied prefixes, replay, and sidechains | Sibling nodes remain siblings; copied native IDs deduplicate shared nodes |
| Append order | JSONL entry order | JSONL line order, including replayed records | Not canonical; parent edges define structure and time/node ID sort siblings for display |
| Non-chat nodes | Tool results, model/thinking changes, compaction, branch summaries, custom entries | Tool use/result, thinking, attachments, system/meta, compact summaries, queue/UI metadata | Preserve structural skeletons only when needed to keep parent chains connected |
| Runtime cursor | RPC `leafId` | `last-prompt.leafUuid` | Optional append-only `cursor` observation for rendering; never node status or Card input |
| Room | Porch knows the selected room profile | Adapter knows its explicitly enrolled project/room | Adapter submits `room`; Neroli validates but does not remap it |

Full-corpus structural inspection deliberately did not print conversation bodies.
Across 624 JSONL files, 77,966 valid rows contained 45,381 distinct UUID-bearing
nodes. There were 7,073 replayed UUID groups (10,541 duplicate occurrences),
including 6,306 groups crossing session containers. Portable content, timestamp,
role, and provider/model facts did not conflict. One UUID had a parent variant:
the later replay placed an existing node below a compaction summary while the
original non-compaction parent remained stable. Therefore a source session is
useful adapter provenance but not canonical node ownership. Resolving replay and
compaction bookkeeping into stable normalized nodes is Claude-adapter work, not
a reason to make Neroli interpret Claude lifecycle semantics.

## Envelope

```json
{
  "format": "neroli-conversation-tree-v1",
  "source": "porch",
  "room": "den",
  "nodes": [
    {
      "native_node_id": "entry-17",
      "native_parent_node_id": null,
      "occurred_at": "2026-08-02T14:20:00Z",
      "kind": "message",
      "source_type": "message",
      "message": {
        "role": "user",
        "content": [
          {"type": "text", "text": "A source message, unchanged."}
        ]
      }
    },
    {
      "native_node_id": "entry-18",
      "native_parent_node_id": "entry-17",
      "occurred_at": "2026-08-02T14:20:02Z",
      "kind": "event",
      "source_type": "thinking_level_change"
    },
    {
      "native_node_id": "entry-19",
      "native_parent_node_id": "entry-18",
      "occurred_at": "2026-08-02T14:20:05Z",
      "kind": "message",
      "source_type": "message",
      "message": {
        "role": "assistant",
        "content": [
          {"type": "text", "text": "A source reply, unchanged."}
        ],
        "provider": "provider-name",
        "model": "provider/model-name"
      }
    }
  ],
  "observations": [
    {
      "native_context_id": "pi-session-123",
      "kind": "cursor",
      "native_node_id": "entry-19",
      "observed_at": "2026-08-02T14:20:06Z",
      "payload": {"runtime": "pi"}
    }
  ]
}
```

## Envelope facts

- `format` is exactly `neroli-conversation-tree-v1`.
- `source` is the stable adapter namespace.
- `room` is the explicitly configured room from which the adapter captured the
  nodes. Neroli validates that the room exists and the source may submit to it;
  Neroli does not translate another route label into a room.
- `nodes` is an additive, idempotent upsert batch. It may contain a complete tree
  or only newly discovered nodes. Missing nodes are not deletions.
- `observations` is optional and defaults to an empty array. Each item is an
  append-only fact that a source context pointed at a node at `observed_at`.

There is intentionally no active head, main-branch flag, continuation marker,
withdrawal state, or branch priority on a node. A `cursor` observation is a
renderer hint beside the tree, not a mutation of the tree.

## Node facts

- `native_node_id` is stable inside `(source, room)`. If upstream IDs are only
  session-local, the adapter scopes them before submission. Native session/file
  membership remains in the adapter's recoverable source journal; it is not
  canonical ownership of a shared tree node.
- `native_parent_node_id` is the normalized parent node, or `null` for a root.
  Each v1 node has at most one parent. The adapter resolves source-specific replay
  and compaction bookkeeping before submission; Neroli does not guess it.
- `occurred_at` is the source occurrence time in UTC when available.
- `source_type` retains the source event category without importing its private
  payload schema.
- `kind` is one of `message`, `tool`, `checkpoint`, or `event`.
- `message` is present only for portable user/assistant content. Its role is
  `user` or `assistant`; v1 content contains ordered `text` blocks. Raw image
  bytes, thinking, tool arguments/results, credentials, and arbitrary extension
  payloads remain in the adapter-owned native journal.

For `(source, room, native_node_id)`, content and normalized parent are immutable.
Identical replay is idempotent. Reusing an ID with different content or parent is
a hard conflict. If an upstream system reuses an ID for a genuinely different
structural occurrence, its adapter scopes a distinct normalized node ID.

After a batch commits, every non-root parent must exist in the same `(source,
room)` forest, every node has at most one parent, and parent edges must be acyclic.
Parents may already exist or arrive in the same batch.

## Observation facts

- `native_context_id` is the source runtime/session context in which the cursor
  was observed. It does not own the canonical nodes it points at.
- `kind` is an open, non-empty string. V1 defines `cursor`; unknown kinds are
  stored and ignored by consumers that do not understand them.
- `native_node_id` must resolve to a node in the same envelope forest.
- `observed_at` is ISO 8601 UTC.
- `payload` is optional free JSON object data. Neroli preserves it but does not
  infer Card or branch semantics from it.

Neroli derives an observation identity from the complete normalized fact, so an
identical replay is idempotent and a later cursor position is a new row. No
observation insert or replay writes `turns` or advances the Card watermark.

## Adapter responsibilities

An adapter:

- selects and submits the configured room;
- translates native node and parent identity;
- filters transcript replay without rewriting conversation content;
- includes structural event skeletons when skipping them would break the parent
  chain;
- retains the complete rich native journal for recovery and future extensions;
- never assigns Card eligibility, branch priority, or memory visibility policy.

### Pi mapping

- Entry `id` / `parentId` -> node / parent identity. The session header remains
  adapter provenance; it may namespace entry IDs if required for global stability.
- `SessionManager.getEntries()` or public RPC `get_entries` supplies append-only
  nodes incrementally.
- User/assistant text -> `message`; tool-only entries -> `tool`; compaction and
  branch summaries -> `checkpoint`; model/thinking/custom lifecycle entries ->
  structural `event` when required by the chain.
- `getLeafId()` is submitted as an optional `cursor` observation with the Pi
  session ID as `native_context_id`. It is used only by history rendering.

This uses only Pi's published executable, exports, RPC, and extension boundaries;
it requires no Pi source modification or fork.

### Claude Code mapping

- Top-level `uuid` / normalized `parentUuid` -> node / parent identity.
- Replayed UUIDs with identical payload are emitted once.
- `sessionId` and JSONL file membership remain adapter provenance. A copied UUID
  is not duplicated merely because it was observed in another session container.
- The adapter resolves later replay/compaction parent bookkeeping into one stable
  source tree before submission. It does not send UI lifecycle interpretations.
- User/assistant text -> `message`; tool use/result -> `tool`; compact summaries
  -> `checkpoint`; attachments and system/meta rows -> structural `event` only
  when needed to keep a parent chain connected.
- `last-prompt.leafUuid`, when the adapter can pair it with an authoritative
  context and observation time, may be submitted as a `cursor` observation. It
  never privileges that branch for Card generation.
- Explicitly enrolled project/room configuration -> envelope `room`.

`src/claude_code_adapter.py` implements this mapping. It emits each UUID once,
hard-fails changed portable content or genuinely ambiguous non-compaction parents,
and chooses the sole stable non-compaction parent for the observed compaction
replay case. It retains ordinary text block boundaries and block-internal
whitespace plus structural tool/checkpoint/event
skeletons; thinking, raw tool payloads, credentials, attachments, and last-prompt
text remain only in the original adapter-owned JSONL. Explicit Claude runtime
injections embedded in user text keep the legacy filter rather than becoming
portable user content.

During the current local migration, Claude Code runs in history-only tree mode:
its canonical nodes and cursor observations are stored, while the pre-existing
legacy Claude loader remains the only projection into Card turns. This prevents
duplicate historical turns/Cards while preserving the already-verified Card
behavior. The split is receiving-instance policy, not a new wire-format feature;
new tree sources normally project directly, and retiring the compatibility path
requires an explicit identity migration.

## Neroli tree layer

Neroli's canonical responsibility is only:

```text
immutable source-neutral nodes
  + one parent edge per node
  + source/room provenance
```

It validates identity, parent consistency, room authorization, and idempotent
replay. It does not infer retry, edit, withdraw, fork, active, abandoned, main, or
continuation meaning.

Schema v12 persists this layer in `conversation_nodes`. Optional source-state
facts live in `conversation_observations`, outside the canonical node relation.

## Card layer

Card generation is a separate derived layer:

- every new conversational branch is equally eligible material;
- when a new branch tail arrives, Card Gen may read its ancestor path as context;
- shared ancestors do not become new trigger material merely because another
  branch reuses them, but Card ranges may include that context again;
- only the new, uncovered branch segment counts toward triggering that processing stream;
- processing-stream attribution and Card membership belong to this derived layer,
  not to the tree, and they are not the same relation;
- path-local rounds may be derived temporarily for model prompting, but they are
  not source-tree facts;
- cards and indexes remain rebuildable from the tree.

The v12 tree compatibility bridge, with the v13 Card-membership correction,
deterministically projects a newly observed tree into Card-only branch sessions.
The first discovered child continues the
existing processing stream; later siblings receive peer streams with the shared
ancestor path marked `turns.is_context=1`. This is arrival-time coverage state,
not canonical branch rank. `conversation_node_branches` attributes new trigger
material to one processing stream. Separately, `card_nodes` records inclusive
Card membership: rolling boundaries and fork context may map one canonical node
to multiple Cards, matching the pre-tree Card Planner semantics.

“Equal” means the same trigger/context/tail-replacement policy applies to every
branch. Shared prefixes alone do not retrigger Card Gen, while the existing
inclusive refeed boundary remains free to describe a boundary message in both the
parent/frozen Card and the new branch Card. None of this names a session parent,
child, main, or continuation in the canonical tree.

## History rendering

`src/history.py::render_history` and `bin/cli.py --render-history` currently return
the full node forest plus the latest observation per `(source, native_context_id, kind)`.
For a cursor, the projection follows parent edges to return its observed path.
Changing only an observation changes that render path and nothing in Card state.
This unbounded CLI projection is diagnostic, not the Porch history UI contract;
a production reader must page a recent context list and lazily load a selected
path/branch instead of materializing an entire room.

## Deliberately deferred

- source-side deletion and tombstones;
- multi-parent DAG support; both inspected sources expose one parent per record;
- portable tool payloads, thinking, raw attachments, and extension state;
- generic Card trigger thresholds and rolling-window tuning.

None of these requires adding head or branch-priority semantics to the tree layer.
