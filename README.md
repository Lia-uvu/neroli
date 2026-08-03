# Neroli
Neroli is a memory infrastructure for long-term AI companions, built on graph-based retrieval. It generates memory cards through the agent's own persona rather than neutral extraction. Time is a first-class citizen: cards stay anchored to when things happened, and recent context takes a separate path from long-term structure. The long-term index clusters cards by event into a hierarchical topic tree and presents information at multiple resolutions — agents dynamically choose the level of detail they need at query time. Per-agent privacy boundaries are enforced over a shared archive.

## How It Works
[LLMs operate fundamentally through persona simulation](https://www.anthropic.com/research/persona-selection-model), so rather than judging what to remember from the outside, Neroli lets the agent inhabit its own identity and context, and decide for itself what matters. Without access to an LLM's internal runtime state, external memory has a hard ceiling — persona-driven generation is one way to approximate what the agent would natively retain, if it could.

```txt
conversation logs
  → messages / turns
      └──→ DB turns watermark → daytime Card Gen (source-independent)
            └──→ event cards (shared / private, per-agent room)
                  │
                  ├──→ cards-last-24        (recent headlines, FIFO)
                  │      └──→ summary agent + recall → summary-last-24 (≤700 chars, verified submit)
                  │
                  └──→ entity resolution    (tags → canonical entities)
                        → weighted graph    (co-occurrence + embedding edges)
                        → recursive Leiden  (hierarchical topic communities)
                              │
                              └──→ curator agent reads tree-change report
                                    → digest.md      (tree projection, hard budget)
                                    → constants.md   (long-lived facts)
```
Storage and index are separate layers. Cards hold the full narrative and privacy markings; the index is a derivative structure that organizes and locates cards, and can be rebuilt from them at any time. A short-context agent condenses recent card headlines and can open the filtered source cards before submitting. A nightly curator inhabits each agent's persona in turn, reads the tree-change report, and rewrites that agent's longer-horizon context.

## Quick Start
There is no setup wizard. You hand [`skills/install/SKILL.md`](skills/install/SKILL.md) to your coding agent (Claude Code, Codex CLI, …) and it does the install: a three-question interview (privacy boundary, model access, existing archives), config filled on your behalf, a checkpoint after every stage, platform-native scheduling. The mechanical steps are prefab scripts (`bin/preflight.sh`, `bin/make-launchd.sh`); the agent's job is the judgment work — your boundary, your persona, your harness.

**Everything that costs money ships off.** First ingest is free and model-less; card generation and the night curator are switches the installer must ask you to flip, with the cost stated first.

## Real Instance
```txt
agents-yard/
├── bedrock/
│   └── recall-pipeline/        shared memory engine + data
│
├── den/                        Room A — Claude Opus
│   ├── cards-last-24.md
│   ├── summary-last-24.md
│   ├── digest.md
│   ├── constants.md
│   ├── toolbox/                room's own tools (diary, todo, mail…)
│   └── slot/                   ← inter-room message box
│
├── loft/                       Room B — Claude Fable
│   ├── cards-last-24.md
│   ├── summary-last-24.md
│   ├── digest.md
│   ├── constants.md
│   ├── toolbox/
│   └── slot/                   ← inter-room message box
│
└── shed/                       shared tools (search, printer, notifications…)
```
Each directory is a room, and each file is an object in it — the folder structure is a text-based mapping of a living space. An agent's room is its living space — everything it has written, built, and accumulated stays between sessions. It walks back in and continues where it left off. slot/ is used for inter-room communication. shed/ holds shared utilities.

The system is iterated on agent feedback, so the primary users of the memory are the agents themselves. 

## Documentation
- [ARCHITECTURE.md](ARCHITECTURE.md) — module map, data flow, boundary rules
- [skills/ops/SKILL.md](skills/ops/SKILL.md) — operations skill and index ("what do I edit to change X")
- [docs/source-adapter-boundary.md](docs/source-adapter-boundary.md) — what source adapters own, what Neroli owns, and the current tree boundary
- [docs/normalized-adapter-contract.md](docs/normalized-adapter-contract.md) — source adapter contract, canonical identity, ordering, and local room policy
- [docs/conversation-tree-adapter-contract.md](docs/conversation-tree-adapter-contract.md) — incremental node/parent tree contract with rendering-only observations
- [schema.md](schema.md) — SQLite schema v13 reference

## FAQ
**Where does my data go?**
Nowhere. Everything is local files and one SQLite database. Model calls go to whatever provider *you* configure (a local CLI, your API key, or Ollama); nothing else leaves the machine.

**What does it cost to run?**
Ingest and retrieval are free (no model calls). Card generation is roughly one small model call per ~14 conversation rounds; the night curator is at most one agentic call per room per night, skipped when nothing new happened. All of it ships disabled until you turn it on.

**What models does it need?**
One chat model for cards (any OpenAI-compatible API, CLI, or Ollama), one embedding model (API or Ollama), and — only if you enable the night curator — an agentic CLI that can write its working directory (codex is the tested default). Memory generation requires real reasoning (the model needs to understand when you're joking, what actually mattered, what was throwaway), so use a model close to your main agent's reasoning ability. Recommended: GPT-5.5 low-thinking. The author runs on an OpenAI Go subscription, which roughly covers it — actual cost depends on conversation frequency and length.

**Does it run anywhere?**
macOS first (launchd scripts included). The engine is plain Python + SQLite and runs on Linux, but the shell helpers use BSD `stat` — adapt before trusting them.

## License
Code: [PolyForm Noncommercial 1.0.0](LICENSE). Docs and media: [CC BY-NC-SA 4.0](LICENSE-DOCS.md). For commercial use, get in touch.
