---
name: skill-creator
description: Use when the user asks to create, write, improve or turn something into a skill (reusable instructions the agent follows for a kind of task).
---

# Creating a skill

A skill is a short set of instructions the agent loads when a task matches its description. Only the
description is always visible, so it decides when the skill is used.

## Steps
1. Ask (or infer from the conversation) what kind of task the skill covers, what a good result looks like, and
   any rules the user cares about (format, tone, tools, things to avoid). If the user has just corrected you
   several times on a task, those corrections are the core of the skill.
2. Pick a name: lowercase words with dashes, e.g. `monthly-report`, `csv-cleanup`.
3. Write the description as one sentence starting with "Use when…", naming the situations and keywords that
   should trigger it. Keep it under 200 characters.
4. Write the instructions in Markdown:
   - a one-line purpose,
   - numbered steps the agent should follow,
   - rules ("always…", "never…") with a short reason for each,
   - an example of a good output if format matters.
   Keep it under about 60 lines: small local models follow short instructions better.
5. Show the draft to the user, then save it with `save_skill`. The app asks the user to approve the save.

## Improving a skill
Load it with `load_skill`, change only what the user asked for (or what their corrections showed), and save it
again with the same name. Earlier versions are kept automatically.
