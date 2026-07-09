# Privacy interview — replaceable template

> For the installing agent: this is the **default** script for the one genuinely personal
> question in the install — where the user draws the share/private line. Adapt the wording
> to your user and your relationship with them; replace this file entirely if your
> deployment has its own standard. What matters is the output, not the script:
> a few lines in the room persona (Step 4) stating the boundary **in the user's own words**.
>
> Never present these examples as the user's policy. They are conversation starters.

## The three questions

1. **What should travel?** What would be fine for another of your agents to know,
   because it helps them support you? (Examples to offer, not impose: ongoing projects,
   schedules, preferences, health facts they'd repeat to any assistant.)

2. **What stays in this room?** What feels like it belongs only between you and this
   agent — things you'd phrase differently, or not mention, to anyone else?
   (Examples: emotional processing, drafts and doubts, money details, other people's
   secrets the user holds.)

3. **When unsure?** Should the card model default to **private** when it can't tell,
   or leave a note for you to sort later? (Recommend: default private. Cards can be
   shared later; leaked context can't be unshared.)

## Where the answers go

- 2–5 lines in `prompts/agent-persona-<room>.md`, appended after the identity lines.
- Phrase as instructions to the card model, e.g. "Anything about X goes in `private`.
  Y-type facts are fine in `share`." Keep it short — it is spent on every card call.
