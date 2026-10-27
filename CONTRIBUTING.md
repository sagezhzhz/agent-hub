# Contributing

Bug reports with `python3 agent_hub.py --check` output are the most useful thing you can send: Agent Hub reads each app's private storage, and every app version can change it.

## Adding another app

Everything lives in `agent_hub.py`. Supporting a new app takes two pieces, and the page needs no changes.

### 1. A reader

A function that returns a list of session dicts, newest first, at most `LIMIT_PER_APP`:

```python
{
    "app": "myapp",            # the key you use in APPS
    "id": "…",                 # the app's own conversation id, used in the deep link
    "title": "…",
    "source": "my-project",    # small line above the title; "" plus a sourceType below for generated labels
    "sourceType": "folder",    # folder | nofolder | group | members | bot
    "projectKey": "folder:/path/to/project",  # cards with the same key share a column on the project board
    "projectName": "my-project",
    "projectKind": "folder",   # folder | nofolder | group | bots
    "preview": "",             # last message; shown on "needs you" cards and in the peek dialog
    "updatedAt": 0,            # ms since epoch
    "runningSince": 0,         # ms since epoch while running, else 0
    "status": "idle",          # running | waiting | idle, or pass idle through finish() to get "unseen"
}
```

For a folder-based app, `**folder_project(cwd)` fills the five source/project fields.

Rules that keep it working on other people's Macs:

- Never write to the app's files. Open SQLite through `open_db()`, which is read-only.
- Don't assume columns exist. Check with `columns()` and build the query from what's there (`first_of()` helps).
- Return `[]` when the app's data folder doesn't exist.
- Raise `FormatError("…")` when the data is there but not in a shape you recognize. The page then says which app couldn't be read instead of quietly showing an empty column.
- Use `finish("idle", finished_at, app_seen_at, "myapp", id)` for finished conversations, so "just finished" works like the other apps.
- Treat a "running" state older than `RUNNING_STALE_SEC` as idle; apps that quit mid-turn leave it behind.

### 2. An `APPS` entry

```python
"myapp": {
    "name": "My App", "reader": myapp_sessions,
    "id": r"[0-9a-f-]{36}",                    # ids allowed into a link; everything else is refused
    "link": "myapp://chat/{id}",               # opens one conversation
    "bundles": [{"ids": ["com.example.myapp"], "names": ["My App.app"]}],
    "letter": "M", "color": "#336699",         # badge when the icon can't be read
},
```

If editions of the app register different URL schemes, put `{scheme}` in `link` and list them in `"schemes"`; the first one the installed app registers is used.

### Finding the link

Most Electron apps handle their deep links in `Contents/Resources/app.asar`. Searching it for the scheme (`grep -a 'myapp://'`) or for `open-url` usually turns up the route that opens a conversation by id. Test it with `open 'myapp://…'` before adding it.

### Before opening a PR

- `python3 agent_hub.py --check` shows the app, a registered link and a conversation count.
- Clicking a card opens that exact conversation.
- `python3 agent_hub.py --demo` still works; add a demo row or two for the new app.
- Update both READMEs: the requirements list, the link list and the status table.
