# Stage 3 — Config, secrets, persona

## Write config

```sh
cp config/rooms.example.json config/rooms.json
cp config/settings.example.json config/settings.json
```

Fill both from the interview. Leave every key you have no answer for at its example default — the defaults are tuned. Then **explicitly set the three spend switches off** (they are the install contract, whatever the example says): `watcher.auto_cards: false`, `nightly.generate_missing_cards: false`, `midlayer.enabled: false`. Show the user a one-screen summary of what you filled.

**Checkpoint:** from the repo root:

```sh
cd src && python3 -c "import config; print(config.ROOMS, config.user_name())" && cd ..
```

prints your room names and the user's name. An exception here means malformed JSON or a missing room dir — fix before continuing.

## Secrets

If anything needs an API key, write `.env` at the repo root (auto-loaded by the engine; `KEY=value` lines):

```sh
CLAUDE_MEMORY_API_BASE_URL=https://…        # OpenAI-compatible base URL (embeddings and/or chat)
CLAUDE_MEMORY_API_KEY=sk-…
# CLAUDE_MEMORY_CODEX_BIN=/path/to/codex     # only if codex is not on PATH (launchd jobs often need this)
# (non-codex CLIs are configured in settings.model_access.cli_cmd, not here)
```

`chmod 600 .env`. Confirm `.env` is in `.gitignore`.

**Checkpoint (one embedding round-trip — the install's only model-touching call):**

```sh
cd src && python3 -c "
from config import load_settings
import embedding
e = load_settings()['embedding']
print(len(embedding.get_embedding('hello', backend=e['backend'], model=e['model'])))" && cd ..
```

prints a vector dimension (e.g. `1024`).

## Card-generation prompt

```sh
cp prompts/gen-cards-prompt.example.md prompts/gen-cards-prompt.md
```

The example is the upstream-maintained, tested default — it works as-is and there is nothing to fill in. The active copy is gitignored only so that a deployment *can* carry its own boundary wording without publishing it; do not present customizing it as a setup task.

## Persona and privacy prompt (per room)

Card generation renders conversations with a short persona preamble so cards are written from the right voice. For each room, create `prompts/agent-persona-<room>.md`: 3–10 lines on who the agent is and how it relates to the user, plus 2–5 lines on this room's share/private boundary.

**Distill, don't re-interview.** You already have both sources: the identity section of the user's existing agent instructions (e.g. their `CLAUDE.md`) and the interview's boundary answers. Draft from those, show the user, let them edit. Keep it short — it's spent on every card call. Write the user's boundary in their words, not Neroli's examples.

**Checkpoint:** the file exists and the user has seen its content.
