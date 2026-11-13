---
name: harness-creator
description: Use when the user wants a new way of working for the agent, e.g. "make me an agent for X", a specialised assistant, a custom workflow or harness.
---

# Creating a harness

A harness decides how the agent works: its instructions, which tools it may use, and how many steps it may take.

Modes:
- `chat`: no tools, one reply. Fastest. For writing, brainstorming, explaining.
- `agent`: uses tools step by step up to `max_steps`. For most tasks.
- `planner`: writes a short plan first, then works through it. For long jobs and scheduled tasks.

Tool groups: `files`, `web`, `python`, `memory`, `skills`, `automation` (schedules and harnesses).
Give only the groups the job needs: fewer tools = shorter prompts = faster and more reliable on a local model.

## Steps
1. Clarify the job, the inputs it works from, and what a finished result looks like.
2. Choose the mode, tool groups and a step limit (6-12 for agent, 12-20 for planner).
3. Write instructions (Markdown, under ~40 lines): role, step-by-step method, quality rules, output format.
4. Save with `save_harness`. Tell the user they can pick it from the harness menu in any chat, or use it in a
   scheduled task.

Fixed multi-stage programs (pipelines, like the Datavera dataset publisher) contain Python code and are added
as zip files or plugins, not with save_harness.
