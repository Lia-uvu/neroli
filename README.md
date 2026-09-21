# Neroli

English | [中文](README.zh-CN.md)

**A local-first long-term memory engine for AI agents**

Neroli turns conversations from multiple sources into durable, time-anchored event Cards, allowing AI agents to carry context across sessions without reloading entire chat histories. It is a working systems project focused on reliable memory pipelines, multi-resolution retrieval, explicit privacy boundaries, and replaceable components.

## Engineering highlights

- **Durable multi-source memory.** Source adapters normalize logs from different runtimes into immutable conversation trees and ordered turns. A rolling generation pipeline turns them into narrative event Cards with time, provenance, tags, and viewer-scoped shared/private content. Cards are then organized into natural event lines, so recall can expand one topic into its complete development over time.
- **Dual-axis retrieval.** Full-text search and chronological context form one axis; a hierarchical event graph built from embeddings, entity co-occurrence, and recursive Leiden community detection forms the other. An agent can search horizontally by keyword or time, or drill from a top-level community into one event line and walk its Card timeline.
- **Fault-tolerant LLM pipelines.** Model output is staged and structurally validated before atomically replacing existing memory. Every physical attempt is audited independently, and malformed or empty responses preserve the last valid result.
- **Decoupled, testable architecture.** Seven modules exchange data through a versioned SQLite contract. Background processing is source-independent, visibility filtering is centralized in storage, MCP exposes only bounded read-only operations, and **144 automated tests** protect key behavior.

## System shape

```text
source adapters
  -> immutable conversation tree + observations
  -> messages / ordered turns
  -> event Cards (headline, narrative, time, provenance, visibility)
       |-> FTS + timeline retrieval
       |-> canonical entities
       |-> weighted Card graph
       |-> adaptive recursive Leiden hierarchy
       |-> recent and long-horizon context projections
  -> viewer-bound CLI / read-only MCP consumers
```

Event Cards are the durable memory layer. FTS tables, entity mappings, graph communities, and generated context files are rebuildable derived views. Storage and indexing are separate, so an indexing experiment can fail, drift, or be replaced without damaging the narrative memory itself.

The canonical store is one SQLite database. Modules exchange data through the persisted schema rather than importing one another; only the top-level orchestration layer composes them.

## Selected design decisions

### Keep the event boundary plastic at the tail

Fixed windows easily cut through the middle of an event. Neroli freezes stable Cards, but re-feeds and rewrites the newest Card when more turns arrive. Existing memory is deleted only after a complete replacement parses successfully, so malformed or empty model output cannot erase a valid result.

### Accept multiple sources and support custom adapters

Neroli can ingest raw JSON/JSONL files from supported sources without requiring users to reshape their history first. To connect another chat platform or agent runtime, developers can implement a source adapter that translates native records into Neroli's public conversation-tree or normalized contract; Card generation, indexing, retrieval, and context generation remain unchanged.

### Let local structure determine graph depth

Each event Card becomes a graph node. Edges combine mean-centered embedding neighbors with rare-entity co-occurrence. Leiden recurses inside each community only when local modularity exceeds a degree-preserving null-model baseline. Coherent regions can remain broad while heterogeneous regions naturally grow deeper levels.

### Agents do not receive the primary database by default

Each Card belongs to an agent workspace and separates shared from private text. By default, an agent receives viewer-filtered context or a bounded read-only retrieval interface—not the primary database. The nightly Curator also runs only inside a filtered workbench copy: invisible Cards are absent, and private fields on visible foreign Cards are blank. Search, recent context, tree snapshots, and Curator exports share the same storage-level visibility rule.

## Modules and tests

The data pipeline is split into seven explicit modules:

- **Ingest** receives multiple sources and stores raw messages, ordered turns, and the conversation tree.
- **History** renders and browses original conversations from the conversation tree.
- **Card Gen** turns newly ingested turns into narrative event Cards.
- **Index** resolves entities, builds the graph, and produces the recursive Leiden event hierarchy.
- **Context** generates recent context that can be injected directly into an agent session.
- **Midlayer** maintains longer-horizon digests and constants over the event hierarchy.
- **Search** provides keyword, time, event-line, and source-text drill-down over those data layers.

Every module has its own automated test coverage, with additional cross-module tests protecting public contracts.

## Design history

Neroli did not begin with its current graph architecture. Early versions explored absolute embedding thresholds, centroid assignment, overlap signals, and several approaches to short- and long-horizon context before narrative storage and retrieval indexing were separated.

The public design history consists of translated, privacy-edited editions of two private working documents. They preserve the experiments, rejected alternatives, and decision sequence while removing personal conversation material:

- [Design history index](DESIGN-HISTORY/README.md)
- [v1: Event memory and vector-space clustering](DESIGN-HISTORY/design-v1.html)
- [v2: Leiden graph index and Midlayer](DESIGN-HISTORY/design-leiden-v2.html)

They explain how the system arrived here; they are not specifications for current behavior. Current facts are defined by the architecture, schema, code, and tests.

## Repository guide

- [Architecture](ARCHITECTURE.md) — module ownership, data flow, and boundaries
- [SQLite schema](schema.md) — persisted structure and migrations
- [Source adapter boundary](docs/source-adapter-boundary.md) — source facts versus Neroli policy
- [Conversation-tree contract](docs/conversation-tree-adapter-contract.md) — canonical nodes, parents, and observations
- [Normalized adapter contract](docs/normalized-adapter-contract.md) — compatibility contract for linear sources
- [Operations index](skills/ops/SKILL.md) — module-owned runbooks

## Running it

Installation uses an agent-assisted workflow rather than a fixed setup wizard. Give [`skills/install/SKILL.md`](skills/install/SKILL.md) to a coding agent; it performs preflight checks, asks for the deployment's privacy and model choices, fills private configuration, and verifies each checkpoint. Tasks that incur model cost ship disabled and must be enabled explicitly.

Run the full test suite with:

```sh
python3 -m unittest discover -s tests -v
```

## License

Code: [PolyForm Noncommercial 1.0.0](LICENSE). Documentation and media: [CC BY-NC-SA 4.0](LICENSE-DOCS.md).
