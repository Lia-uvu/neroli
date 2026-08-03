# Source adapter boundary

This document separates source translation from Neroli's two layers. It describes
the implemented tree boundary and the compatibility behavior retained for the
older `neroli-normalized-v2` contract.

The short version is:

```text
harness records
  -> adapter-owned source facts
  -> Neroli plain conversation tree
  -> Card generation and history rendering
```

An adapter declares which explicitly configured room produced its messages; it
does not create rooms, define room permissions, own resident identity, choose
Card policy, or control the Neroli schema. Neroli validates the submitted room and
enforces its local privacy and memory policy. The canonical tree stores nodes and
parent edges without deciding which branch is main, active, continued, preferred,
or abandoned. Optional runtime cursor facts are stored as observations beside the
tree and may affect rendering only. Neroli does not parse harness-private journals
or infer source facts that the source cannot authoritatively provide.

## Responsibility boundary

| Concern | Source adapter | Neroli tree layer | Neroli Card layer |
|---|---|---|---|
| Native identity | Choose stable source and node identifiers; keep source session/file membership in its recoverable journal | Namespace and persist immutable nodes | Consume canonical node identity; do not invent another transcript identity |
| Captured content | Preserve source message text, role, and occurrence time without rewriting it; keep source line order for audit | Validate and persist portable content | Read eligible content as input; cards remain derived and rebuildable |
| Conversation structure | Normalize each node to at most one authoritative parent | Persist a forest of roots, nodes, and parent edges | Traverse ancestor paths for context and new branch tails for owned material |
| Branch status | Do not translate runtime leaf/head selection into memory semantics | Store no main, active, continued, withdrawn, preferred, or abandoned flag | Treat every captured branch equally |
| Runtime observation | Submit optional context + node + time when the source exposes it authoritatively | Store it outside nodes; never advance the Card watermark | History may choose an observed path; Card Gen never reads it |
| Shared prefix | Reuse stable native node IDs instead of copying them into new identities | Store one node with its parent edge | Reuse ancestors as context; do not card the same shared material again |
| Source-specific events | Keep structural skeletons needed for parent chains; retain the rich native journal outside the portable contract | Accept only public contract fields; do not depend on a harness journal format | Do not interpret harness lifecycle events as Card policy |
| Room origin | Submit the configured room the adapter explicitly served | Validate that the room exists and the source may submit to it; do not remap it | Apply local visibility and memory policy for that room |
| Retry | Replay stable native facts; never reuse an ID for changed content or parent | Make identical replay idempotent and reject immutable identity conflicts | Wake only for actual new eligible tree material |

Adapter-provided parent IDs are source facts, not UI interpretations. A shared
prefix or parent edge does not prove whether the user selected retry, edit,
withdraw, clone, or fork. The tree does not need that intent even when a source UI
exposes it.

This is intentionally a two-layer Neroli boundary. The tree is the canonical
record. Cards, FTS, clusters, and history projections are downstream views; none
of them may promote a runtime-selected path into canonical branch status.
Observations are retained source facts, but their consumer is rendering; Card
ownership continues to come only from tree nodes.

Branch equality still requires derived processing state: Card generation must
know which nodes are already covered and which uncovered tail it owns. That state
belongs to the Card layer. It is not a main/active/continuation property of the
canonical forest.

## Implemented tree input (schema v12)

`neroli-conversation-tree-v1` accepts additive immutable nodes, at most one parent
per node, adapter-owned room origin, and optional append-only observations. It
supports incremental node-only delivery; every referenced parent must exist after
the batch, and cycles or immutable ID reuse are hard failures.

Portable user/assistant nodes are projected into `messages` / `turns` for the
existing Card engine. Shared ancestors in peer branch sessions are marked
`is_context=1`; it does not count as new trigger material, but inclusive Card
boundaries and fork context may legitimately map the same canonical node to more
than one Card through `card_nodes`.
Observation-only updates write no turns and do not wake Card Gen.

## What normalized v2 input means today

The current input is a collection of immutable message nodes with optional parent
edges. It is not a nested JSON tree object. `native_parent_message_id` describes a
message edge; `native_parent_session_id` describes source session lineage. A
linear conversation is the simplest tree.

The current persisted model can preserve:

- the same canonical message appearing in more than one session occurrence;
- copied prefixes, when the adapter reuses the original `native_message_id` values;
- optional message-parent and session-parent provenance;
- divergent sessions whose shared occurrences can be projected into
  `session_forks` processing evidence.

For the implemented v2 loader, adapters must currently submit a complete known
ordered trajectory for each native session included in an updated delivery. The
loader derives `round` and `message_seq` from the messages present in that
delivery. A delta containing only newly appended nodes would restart derived
round numbering and is not yet a supported update mode.

The delivery is replayable but not an authoritative replacement snapshot:
omitting an older node does not delete its existing `turns` occurrence. There is
currently no tombstone, source deletion, or branch replacement operation.

Normalized v2 cannot safely flatten every sibling path from a harness event tree into
one linear native session. Its compatibility adapter exports a linear trajectory
or copied-prefix sessions. The tree contract removes that limitation by
submitting sibling nodes and parent edges directly, without marking a main or
active branch.

This describes compatibility `neroli-normalized-v2`. The source-neutral
incremental node/parent + observation format is documented separately in the
[conversation tree adapter contract](conversation-tree-adapter-contract.md).

## Adapter obligations for v2

- `source` is a stable adapter namespace, not a model or room name.
- Current v2 uses `source_route` plus receiving-instance route mapping as a
  compatibility mechanism. The tree boundary replaces that with
  an adapter-submitted `room` which Neroli validates without remapping.
- `native_session_id` is stable source identity, not a spool path.
- `native_parent_session_id`, when present, is the stable native ID of the parent
  session, not a filename or local filesystem path.
- `native_message_id` is globally stable inside one `source`. If the upstream ID
  is only session-local, the adapter scopes it before submission.
- `native_parent_message_id`, when present, names the source message parent. It
  must not point at an omitted harness-only event such as a compaction or tool
  lifecycle record. If the adapter cannot map to a message parent without
  guessing, it omits the field.
- Copied history keeps the same native message IDs across sessions. An actual
  edited message receives a new ID.
- `source_sequence` is stable and unique within a native session. It is source
  order, not a Neroli round number.
- The adapter retains any richer source journal needed for recovery, audit, or a
  future contract extension.

The concrete JSON fields and validation rules are defined by the
[normalized adapter contract](normalized-adapter-contract.md).

## Neroli obligations for v2

- Reject malformed envelopes and unknown source routes as a whole.
- Generate canonical IDs independently of spool filenames and delivery paths.
- Preserve immutable message content and native provenance.
- Store ordered occurrences separately from deduplicated message content.
- For current v2, resolve the compatibility route through receiving-instance
  policy. For the tree contract, validate the adapter-submitted room and
  apply local privacy policy without changing its origin.
- Keep identical replay idempotent and wake downstream Card processing only for
  actual `turns` changes.
- Treat inferred shared-prefix forks as processing evidence, not authoritative
  UI intent.

## Not implemented by either current contract

The following remain outside both implemented contracts and must not be inferred
from their fields:

- branch withdrawal, replacement, tombstones, or source-side deletion;
- edit/retry/fork reason codes;
- a generic multi-parent DAG;
- portable raw tool payloads, thinking, and attachments.

Adding any of these changes persistence or long-term interpretation. It requires
an explicit versioned contract/schema decision rather than an adapter-local
convention.
