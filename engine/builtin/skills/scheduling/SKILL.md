---
name: scheduling
description: Use when the user wants something to happen automatically or regularly ("every morning", "each week", "remind me", "keep checking").
---

# Setting up a scheduled task

Scheduled tasks run a prompt on a schedule in their own chat while the app is running (it can start with
Windows and stay in the tray).

1. Agree on: what should happen, how often, and where results go (a chat is created each run; files can be
   written to the project's folders).
2. Write the task prompt so it works with nobody watching: state the goal, the folder or sources to use,
   what to produce, and what to do if something fails ("if no new data, say so and stop").
3. Choose the schedule in plain words: "every day at 08:30", "weekdays at 9", "every 3 hours",
   "every monday at 10:00".
4. Create it with `create_schedule`. Mention that runs which change files ask for approval unless the user
   turns on "approve automatically" for that task on the Schedules page.
