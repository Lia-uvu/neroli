# Neroli: Technical Overview

Neroli is a local-first continuity engine for long-running AI agents. It is designed
for agents that need to carry useful context across days of everyday coordination,
knowledge work, recurring routines, ongoing projects, or personal interaction without
placing an entire transcript back into every prompt.

The system turns source conversations into durable **event Cards**, organizes those
Cards as a rebuildable hierarchical graph index, and projects the result into small
context views that an agent can inspect at different time scales. It supports multiple
agents over one archive while enforcing viewer-specific visibility at storage and tool
boundaries.

This document describes the implemented design. It is intentionally independent of a
particular model provider, chat UI, or agent harness.

## The problem Neroli is solving

Long-running agents face several different memory problems that are often collapsed
into one vector database:

- raw transcripts are complete but too large and noisy to inject continuously;
- isolated extracted facts lose the narrative arc that makes an event intelligible;
- semantic similarity alone does not provide chronology or stable topic structure;
- recent activity needs more detail than old material;
- a model-generated summary can drift if it repeatedly rewrites its previous wording;
- multi-agent archives need enforceable privacy boundaries, not instructions that ask
  the model to ignore data it has already received.

Neroli separates these concerns. Source capture, event memory, indexing, retrieval,
recent context, long-horizon context, and access control are different layers with
small contracts between them.

```text
source adapters
  -> immutable conversation nodes + optional rendering observations
  -> messages / ordered turns
  -> event Cards
       |-> FTS and timeline retrieval
       |-> canonical entities
       |-> weighted Card graph
       |-> adaptive recursive Leiden hierarchy
       |-> recent and long-horizon context projections
  -> viewer-bound CLI / MCP consumers
```

The canonical store is one SQLite database. Module-to-module exchange happens through
its schema rather than through cross-module imports. The Card narratives are durable;
the FTS index, entity map, graph communities, and generated context files are derived
views that can be rebuilt.

## 1. Source-neutral conversation capture

Neroli does not make an agent runtime's session format its own data model. A source
adapter translates native records into one of two public contracts:

- [`neroli-conversation-tree-v1`](docs/conversation-tree-adapter-contract.md) accepts
  incremental immutable nodes, at most one authoritative parent per node, and optional
  append-only observations;
- [`neroli-normalized-v2`](docs/normalized-adapter-contract.md) remains a compatibility
  path for sources that deliver complete linear trajectories.

The tree contract stores a plain conversation forest. It does not label a branch as
main, active, preferred, continued, abandoned, or withdrawn. A parent edge is a source
fact, not an inferred explanation of what a user did in a particular interface.
Missing structure is left missing rather than guessed from timestamps or array order.

Runtime cursor state is stored separately as an observation. A renderer may use a
cursor observation to highlight the path selected at that moment, but an observation
cannot write turns, advance the Card-generation watermark, or change Card ownership.
This prevents transient UI state from silently becoming long-lived memory policy.

Adapters declare the explicitly configured agent workspace that produced a record;
Neroli validates that origin and then applies local memory and privacy policy. Adapters
do not create workspaces, grant visibility, decide what deserves a Card, or interpret
source-specific lifecycle events for downstream consumers. The detailed responsibility
split is documented in the [source adapter boundary](docs/source-adapter-boundary.md).

## 2. Event Cards as the narrative memory layer

The raw conversation layer is preserved, but the main unit of long-term recall is an
event Card:

```text
Card = time range + headline + share text + private text + tags + origin
```

A Card describes a natural event arc rather than a fixed-size chunk. For a daily
assistant, that arc might be a decision, errand, routine, or plan. For a project agent,
it might be a design discussion, incident, experiment, or rejected approach.

Card generation is performed in the operating context supplied for the target agent.
This is a deliberate alternative to neutral third-party extraction: what matters is
partly role-dependent. The same conversation may contain a durable constraint for one
agent and disposable detail for another. The application can supply an agent profile,
while the database continues to enforce what that agent is allowed to read.

### Rolling freeze/refeed

Fixed windows tend to cut through events. Neroli instead keeps the newest Card
plastic while freezing stable history. If the current Cards are `A, B, C`:

```text
frozen Cards:       A, B
mutable tail:       C
raw context starts: B.turn_end (inclusive)
successful output: replaces C only
```

The overlap at `B.turn_end` is intentional. The model can reconsider how the end of
one event connects to new material without repeatedly rewriting the entire history.
During initial generation, no prefix is frozen until the model has produced at least
two Cards; this avoids forcing an arbitrary split before an event boundary exists.

For conversation branches, shared ancestors may be read as context but do not count as
new trigger material merely because another branch reuses them. Trigger, readable
context, and replacement ownership remain separate concepts. Inclusive boundaries can
therefore associate one canonical node with more than one Card without making the node
globally owned by either Card.

### Failure safety

Each physical model attempt is audited separately from logical success. Existing tail
Cards are deleted only after the complete replacement parses successfully. A malformed
or empty response is a hard failure: the old memory remains intact, the attempt stays
auditable, and later processing can retry. This keeps a paid model call from becoming a
partially committed memory mutation.

The implementation is in [`src/gen_cards.py`](src/gen_cards.py); operational behavior
and configurable policy are separated in
[`skills/ops/card-gen.md`](skills/ops/card-gen.md).

## 3. From Cards to a multi-resolution topic graph

Cards preserve narrative. The graph index exists to organize and locate them, not to
replace their text.

### 3.1 Conservative entity resolution

Card tags are useful graph signals but are noisy: synonyms fragment a topic, while
over-aggressive merging destroys distinctions. Neroli promotes only recurring tags to
canonical-entity resolution. Embedding similarity produces a small candidate set; an
optional conservative language-model judge decides whether a new tag names the same
entity as one of those candidates. Candidate omission, duplicate verdicts, malformed
JSON, and incomplete batches fail closed instead of silently creating a false merge.

With the shipped defaults, a tag is considered after appearing on at least two Cards,
candidate entities require cosine similarity of at least `0.6`, and at most five
candidates are shown to the judge. These are deployment settings, not architectural
constants. Tags with no plausible candidate become new entities directly. Resolved
mappings are persistent and incremental, so a steady-state rebuild makes no judge call
when there are no new tags.

See [`src/entity_resolve.py`](src/entity_resolve.py).

### 3.2 Weighted Card graph

Each Card is a graph node. Candidate edges are the union of two signals:

1. **semantic backbone** — top-*k* neighbors by cosine similarity over mean-centered
   Card embeddings;
2. **entity co-occurrence** — every pair of Cards sharing a canonical entity, weighted
   more strongly for rare entities.

For an entity `e` that appears on `df(e)` Cards out of `N`, its contribution is:

```text
idf(e) = log(N / df(e))
cooc(i, j) = sum(idf(e)) for entities shared by Cards i and j
```

Stop entities and entities appearing on more than a configured fraction of the corpus
are removed. This prevents universal names and generic concepts from connecting nearly
everything.

Embedding cosine is min-max normalized over the candidate edge set; co-occurrence is
normalized by the largest co-occurrence score. The final edge weight is:

```text
w(i, j) = w_emb * normalized_cosine(i, j)
        + w_cooc * normalized_cooccurrence(i, j)
```

The shipped defaults use `k = 12` and equal signal weights. Embeddings provide broad
connectivity even when tags do not overlap; rare shared entities sharpen topic
separation. The implementation is intentionally independent of the community-detection
module: [`src/graph.py`](src/graph.py) returns only Card IDs and weighted edges.

### 3.3 Adaptive recursive Leiden

A single global distance threshold tends to over-split coherent regions and under-split
heterogeneous ones. Neroli runs Leiden community detection at the top level, then
recurses into each community's induced subgraph only when the proposed local split is
stronger than structure found by chance.

For each candidate subgraph:

1. run Leiden and measure observed modularity `Q_observed`;
2. create an ensemble of degree-preserving rewired versions of the same subgraph;
3. run Leiden on every rewire to obtain a null modularity distribution;
4. accept the split only when

```text
Q_observed > mean(Q_null) + z * std(Q_null)
```

The default ensemble uses 20 rewires and `z = 2.0`. A fixed random seed makes an
unchanged input reproducible. The result is a hierarchy whose depth adapts to local
structure rather than a predetermined number of levels.

Every Card receives one primary leaf. It may also receive a bounded number of secondary
leaf memberships when enough of its graph neighbors lie across another topic boundary.
This keeps navigation tree-shaped while representing events that genuinely touch more
than one subject.

See [`src/community.py`](src/community.py) and the
[Index operations note](skills/ops/index.md).

## 4. Retrieval treats topic and time as different axes

Time is stored on Cards and used for timelines, ranges, surrounding context, recent
context, and resurfacing. It is not mixed into community discovery: two events are not
placed in the same topic merely because they happened recently.

The retrieval layer supports several complementary paths:

- hierarchy drill-down from community to child community to Card timeline;
- FTS keyword search, with multi-term AND results followed by OR backfill;
- Card detail and bounded surrounding Cards from the same source session;
- time-range queries;
- optional semantic search as a fallback, not the only memory interface;
- low-frequency resurfacing that favors older, less-opened Cards.

Raw turns are not general keyword-search material. They can be opened only through an
eligible same-workspace Card. This keeps transcript access more restricted than Card
access and makes search results explainable through headlines, snippets, tags, and
Card IDs.

For agent runtimes, the read-only MCP surface exposes keyword search, Card detail,
same-room raw turns, bounded session context, time browsing, and low-frequency
resurfacing of older Cards. Search and browse calls can expand a bounded number of
results in one round trip. Each tool call declares its viewer; the SQLite connection
uses read-only and `query_only` modes. Semantic/model-backed search, canonical History,
ingestion, and memory writes are outside that surface. See
[`src/mcp_server.py`](src/mcp_server.py).

## 5. Two time scales of generated context

Neroli does not ask one summary to serve both immediate activity and long-term
orientation.

### Recent layer

`cards-last-24.md` is a deterministic, bounded headline ledger built from visible
Cards. A separate short-summary agent may inspect those Cards through a filtered
workbench and submit a tightly bounded `summary-last-24.md`. Its output passes a
deterministic schema and length check before the canonical file is replaced; failure
preserves the previous summary.

### Long-horizon layer

The nightly Midlayer snapshots the current hierarchy and builds two inputs:

- a fresh-Card report, where timestamps—not changes in clustering topology—define
  what is new;
- a full-tree view that shows the historical shape of each topic line.

An isolated curator workbench then produces a compact `digest.md` plus incremental
long-lived constants. The digest is rewritten from the current tree and source Cards,
not from yesterday's prose. This avoids summary-on-summary drift, hardened wording,
and error inheritance. The previous successful digest remains an audit artifact and a
freshness watermark, not a prompt input.

Model output is staged through a submission artifact and checked again by the parent
process. The model can write only inside its temporary workbench; it cannot directly
write the canonical database or final context files.

See [`src/last24.py`](src/last24.py), [`src/treesnap.py`](src/treesnap.py), and
[`src/curator.py`](src/curator.py).

## 6. Privacy is a data-path property

Each Card has an origin plus `share` and `private` text. The central visibility rule is:

- an agent can see Cards from its own workspace;
- it can see another workspace's Card only when that Card has non-empty shared text;
- private text and source turns are available only to the owning workspace.

The predicate is implemented once in the storage layer and reused by Search, recent
context, tree snapshots, and curator exports. Keyword search evaluates shared fields
for all visible Cards but private text only for the owner. Cross-workspace semantic
search uses share-safe vectors rather than full-text vectors.

The curator does not receive the canonical database. It receives a filtered copy in
which invisible Cards are absent and private fields on shared foreign Cards are blank.
The MCP server applies the same storage predicate to the viewer declared by each call.
This keeps field filtering deterministic, while viewer identity is a caller-honored
boundary rather than an authenticated claim.

Two current limitations are documented rather than hidden: Card tags may be derived
from private text and can currently participate in cross-workspace tag matches for an
otherwise visible Card; stored cluster labels may also contain derived entity clues.
Tightening either path requires an explicit privacy-design change.

## 7. Replaceable modules and operational boundaries

The implementation is split into Ingest, Card Gen, History, Index, Context, Search,
and Midlayer modules. Modules may depend on the SQLite storage layer and leaf utilities,
but not on one another; only the orchestration layer composes them. This keeps the
public database schema as the contract and allows one derived module to fail or be
replaced without changing the Card store.

Model calls are limited to jobs that require judgment: Card generation, ambiguous
entity resolution, recent summarization, and long-horizon curation. Ingestion, source
identity validation, FTS, graph construction after embeddings, visibility filtering,
watermarks, submission gates, and most projections are deterministic. Model-backed
features are separately switchable and audited.

The codebase includes module-owned tests and cross-module contracts for adapter
idempotence, immutable identity, tree/observation isolation, source independence,
Card tail-rewrite safety, privacy-filtered retrieval, read-only MCP behavior, and
Midlayer submission gates. Direct characterization tests for graph-weight construction,
recursive split significance, secondary membership, and some semantic privacy paths
remain explicit gaps.

## Current scope

Neroli is a working, self-hosted system rather than a polished hosted product. The
engine is Python and SQLite; bundled scheduling and setup helpers are macOS-first.
Source-side deletion/tombstones, multi-parent DAGs, and generic graph traversal are not
implemented. A tree node has at most one authoritative parent, and missing nodes in a
later delivery are not interpreted as deletions.

For the current module map, see [`ARCHITECTURE.md`](ARCHITECTURE.md). For persisted
fields and migrations, see [`schema.md`](schema.md). For installation, operations, and
the public adapter contracts, start from the [documentation index](README.md#documentation).
