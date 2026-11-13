---
name: datavera-account
description: Use for questions about the user's Datavera account — their datasets, views and analytics, comments to answer, status of submissions, or companies.
---

# Working with a Datavera account

Tools (connector "Datavera"): `whoami`, `list_my_datasets`, `get_my_dataset`, `get_my_analytics`,
`get_dataset_analytics`, `list_comments`, `reply_to_comment`, `list_my_companies`, `search_datasets`.

- Start with `whoami` if you do not yet know the username or companies.
- `list_my_datasets` is paged (`page`, `per_page`); stop paging as soon as you have what you need — never page
  through everything just to count. Ask for a status filter when possible.
- Analytics: report the period, the top datasets and the change, in a short table.
- Comments: summarise who said what. Draft replies and show them; only call `reply_to_comment` after the user
  approves the wording (the app also asks before sending).
- Datasets in `pending_review` are with moderators; `draft` ones can still be edited (give the edit URL).
