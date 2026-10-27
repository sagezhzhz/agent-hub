#!/usr/bin/env python3
"""Agent Hub: one local page listing AI agent conversations from your desktop apps.

Reads each app's local data read-only, serves a dashboard on 127.0.0.1,
and jumps back into the right app via its URL scheme when a card is clicked.
It writes agent_hub_state.json next to this script, which remembers which
finished conversations you have already looked at, and caches each app's icon
as a PNG in ~/Library/Caches/agent-hub.
Python 3.8+ standard library only.

    python3 agent_hub.py            start and open the browser
    python3 agent_hub.py --check    report what was found for each app, then exit
    python3 agent_hub.py --demo     show made-up conversations instead of your apps
    python3 agent_hub.py --no-open  don't open the browser

To support another app, write a reader that returns session dicts (see
CONTRIBUTING.md) and add an entry to APPS at the bottom of the readers.
"""

import sys

if sys.version_info < (3, 8):
    sys.exit("Agent Hub needs Python 3.8 or newer (found %d.%d)." % sys.version_info[:2])

import base64
import glob
import json
import os
import platform
import plistlib
import re
import sqlite3
import subprocess
import threading
import time
import urllib.request
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST = "127.0.0.1"
DEFAULT_PORT = int(os.environ.get("AGENT_HUB_PORT", "47321"))
PORT = DEFAULT_PORT  # the port actually bound, set in main()
HOME = Path.home()
APP_SUPPORT = HOME / "Library" / "Application Support"
PAGE = Path(__file__).with_name("agent_hub.html")
STATE_FILE = Path(__file__).with_name("agent_hub_state.json")
ICON_CACHE = HOME / "Library" / "Caches" / "agent-hub"

LIMIT_PER_APP = 80
RUNNING_STALE_SEC = 30 * 60   # a "started" turn older than this is treated as stale, not running
FIRST_RUN_LOOKBACK_SEC = 6 * 3600  # on first launch, only the last few hours count as "unseen"

DEMO = "--demo" in sys.argv


class FormatError(Exception):
    """The app's data is there but not in a shape this version understands."""


def now_ms():
    return int(time.time() * 1000)


# ---------------------------------------------------------------- seen state

class SeenState:
    """Which finished conversations the user has opened from the hub."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            data = {}
        self.baseline = data.get("baselineAt") or now_ms() - FIRST_RUN_LOOKBACK_SEC * 1000
        self.seen = data.get("seen") or {}
        if "baselineAt" not in data and not DEMO:
            self._save()

    def seen_at(self, app, sid):
        return max(self.seen.get(f"{app}:{sid}", 0), self.baseline)

    def mark(self, app, sid):
        with self.lock:
            self.seen[f"{app}:{sid}"] = now_ms()
            self._save()

    def _save(self):
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"baselineAt": self.baseline, "seen": self.seen}))
            tmp.replace(self.path)
        except OSError:
            pass  # a read-only checkout still works; "viewed" just isn't remembered


STATE = SeenState(STATE_FILE)


def finish(status, updated_at, seen_after, app, sid):
    """Turn an idle conversation into "unseen" when it changed after you last looked."""
    if status == "idle" and updated_at > max(seen_after, STATE.seen_at(app, sid)):
        return "unseen"
    return status


# ---------------------------------------------------------------- shared helpers

def open_db(path):
    """Open an app's SQLite file without ever writing to it.

    Some apps hold their database in a way that makes a plain read-only open fail;
    immutable=1 still reads it (possibly a moment out of date).
    """
    for mode in ("mode=ro", "immutable=1"):
        try:
            con = sqlite3.connect(f"file:{path}?{mode}", uri=True, timeout=2)
            con.execute("select 1 from sqlite_master limit 1")
            return con
        except sqlite3.Error:
            continue
    raise FormatError(f"can't open {path}")


def columns(con, table):
    cols = {row[1] for row in con.execute(f"pragma table_info({table})")}
    if not cols:
        raise FormatError(f"no '{table}' table")
    return cols


def first_of(cols, *names, default="''"):
    """SQL expression for the first of `names` that exists, skipping empty strings."""
    present = [f"nullif({n},'')" for n in names if n in cols]
    return f"coalesce({', '.join(present)})" if len(present) > 1 else (present[0] if present else default)


def folder_project(cwd):
    if not cwd or "/scratch-workspaces/" in cwd:
        return {"source": "", "sourceType": "nofolder", "projectKey": "folder:none", "projectName": "", "projectKind": "nofolder"}
    name = Path(cwd).name or cwd
    return {"source": name, "sourceType": "folder", "projectKey": f"folder:{cwd}", "projectName": name, "projectKind": "folder"}


def first_line(text):
    lines = (text or "").strip().splitlines()
    return lines[0][:120] if lines else ""


def strip_md_links(text):
    return re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)


def iso_ms(ts):
    try:
        return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return 0


# ---------------------------------------------------------------- Codex

CODEX_HOME = Path(os.environ.get("CODEX_HOME") or HOME / ".codex")
CODEX_HIDDEN_SOURCES = ("guardian_review", "subagent")
TURN_EVENT = re.compile(r'"timestamp":"([^"]+)".*"payload":\{"type":"(task_started|task_complete|turn_aborted)"')


def codex_db():
    """Newest state_<n>.sqlite: the number goes up when Codex changes its schema."""
    found = []
    for p in CODEX_HOME.glob("state_*.sqlite"):
        m = re.fullmatch(r"state_(\d+)\.sqlite", p.name)
        if m:
            found.append((int(m.group(1)), p))
    return max(found)[1] if found else None


def codex_turn_state(rollout_path):
    """Last turn lifecycle event in the rollout file and when it happened."""
    try:
        with open(rollout_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 256 * 1024))
            tail = f.read().decode("utf-8", "replace")
    except (OSError, TypeError):
        return None, 0
    for line in reversed(tail.splitlines()):
        if '"event_msg"' not in line:
            continue
        m = TURN_EVENT.search(line)
        if m:
            return m.group(2), iso_ms(m.group(1))
    return None, 0


def codex_sessions():
    db = codex_db()
    if not db:
        return []
    con = open_db(db)
    try:
        cols = columns(con, "threads")
        if not {"id", "cwd"} <= cols:
            raise FormatError(f"{db.name}: threads table has no id/cwd")
        updated = "coalesce(updated_at_ms, updated_at*1000)" if "updated_at_ms" in cols else "updated_at*1000"
        recency = f"coalesce(nullif(recency_at_ms,0), {updated})" if "recency_at_ms" in cols else updated
        where = ["archived = 0"] if "archived" in cols else []
        params = []
        if "thread_source" in cols:
            where.append(f"coalesce(thread_source,'') not in ({','.join('?' * len(CODEX_HIDDEN_SOURCES))})")
            params += CODEX_HIDDEN_SOURCES
        rows = con.execute(
            f"""select id, {first_of(cols, 'name', 'title', 'first_user_message')}, cwd, {updated},
                       {'rollout_path' if 'rollout_path' in cols else 'null'},
                       {'preview' if 'preview' in cols else "''"}
                from threads {('where ' + ' and '.join(where)) if where else ''}
                order by {recency} desc limit ?""",
            (*params, LIMIT_PER_APP),
        ).fetchall()
    finally:
        con.close()

    out = []
    for tid, title, cwd, updated_ms, rollout, preview in rows:
        updated_ms = updated_ms or 0
        state, state_at = codex_turn_state(rollout)
        running_since = 0
        if state == "task_started" and now_ms() - updated_ms < RUNNING_STALE_SEC * 1000:
            status, running_since = "running", state_at or updated_ms
        else:
            status = "idle"
            if state == "task_complete":
                status = finish("idle", state_at or updated_ms, 0, "codex", tid)
        out.append({
            "app": "codex", "id": tid, "title": first_line(title), **folder_project(cwd),
            "preview": (preview or "").strip()[:240], "updatedAt": updated_ms,
            "runningSince": running_since, "status": status,
        })
    return out


# ---------------------------------------------------------------- Claude (Code tab)

CLAUDE_SESSIONS = APP_SUPPORT / "Claude" / "claude-code-sessions"


def claude_sessions():
    out = []
    for path in glob.glob(str(CLAUDE_SESSIONS / "*" / "*" / "local_*.json")):
        try:
            d = json.loads(Path(path).read_text())
        except (OSError, ValueError):
            continue
        sid = d.get("sessionId") if isinstance(d, dict) else None
        if not sid or d.get("isArchived"):
            continue
        waiting_dir = Path(path).parent / "waiting-input"
        user_at = d.get("latestUserFrameAt") or 0
        edge_at = d.get("lastTurnEdgeAt") or 0
        summary = d.get("postTurnSummary") if isinstance(d.get("postTurnSummary"), dict) else {}
        running_since = 0
        if user_at > edge_at and now_ms() - user_at < RUNNING_STALE_SEC * 1000:
            status, running_since = "running", user_at
        elif (d.get("interruptedAwaitingPerson") or summary.get("needs_action")
              or (waiting_dir.is_dir() and any(sid in p.name for p in waiting_dir.iterdir()))):
            status = "waiting"
        else:
            # Claude records when you last looked at the session; finished after that = unseen.
            status = finish("idle", edge_at, d.get("lastFocusedAt") or 0, "claude", sid)
        out.append({
            "app": "claude", "id": sid, "title": first_line(d.get("title")), **folder_project(d.get("cwd", "")),
            "preview": (summary.get("needs_action") or summary.get("status_detail") or "")[:240],
            "updatedAt": max(d.get("lastActivityAt") or 0, user_at, edge_at),
            "runningSince": running_since, "status": status,
        })
    out.sort(key=lambda s: s["updatedAt"], reverse=True)
    return out[:LIMIT_PER_APP]


# ---------------------------------------------------------------- Grok Bot

GROK_STORE = APP_SUPPORT / "Grok Bot" / "sand-client-persistence"


def b32key(name):
    s = name.upper()
    return base64.b32decode(s + "=" * (-len(s) % 8)).decode()


def grok_read(suffix):
    """Find the persisted slice whose decoded key ends with `suffix` (any signed-in account)."""
    for f in GROK_STORE.glob("*.blob"):
        try:
            key = b32key(f.stem)
        except (ValueError, UnicodeDecodeError):
            continue
        if key.startswith("sand.client.slice.account.") and key.endswith(suffix):
            try:
                data = json.loads(f.read_text())
            except (OSError, ValueError):
                return None
            return data.get("value") if isinstance(data, dict) else None
    return None


def grok_sessions():
    if not GROK_STORE.is_dir():
        return []
    roster = grok_read(".roster.last-roster")
    if roster is None:
        if any(GROK_STORE.glob("*.blob")):
            raise FormatError("no conversation list found in Grok Bot's local storage")
        return []
    rows = [r for r in (roster.get("rows") or []) if isinstance(r, dict) and r.get("id")]
    # A bot that belongs to a group is filed under that group's project.
    parent = {}
    for r in rows:
        if r.get("isGroup"):
            for m in r.get("memberIds") or []:
                parent.setdefault(m, r)
    out = []
    for r in rows:
        if r.get("isHiddenFromSidebar"):
            continue
        entry = r.get("lastEntry") if isinstance(r.get("lastEntry"), dict) else {}
        activity = max(r.get("lastActivityAt") or 0, r.get("updatedAt") or 0)
        if r.get("awaitingUserResponse"):
            status = "waiting"
        elif r.get("hasUnread"):
            status = finish("idle", activity, 0, "grok", r["id"])
        else:
            status = finish("idle", r.get("lastActivityAt") or 0, r.get("lastViewedAt") or 0, "grok", r["id"])
        if r.get("isGroup"):
            project = {"source": "", "sourceType": "members", "memberCount": len(r.get("memberIds") or []),
                       "projectKey": f"grok:{r['id']}", "projectName": r.get("name") or "", "projectKind": "group"}
        elif r["id"] in parent:
            g = parent[r["id"]]
            project = {"source": g.get("name") or "", "sourceType": "group",
                       "projectKey": f"grok:{g['id']}", "projectName": g.get("name") or "", "projectKind": "group"}
        else:
            project = {"source": "", "sourceType": "bot", "projectKey": "grok:bots", "projectName": "", "projectKind": "bots"}
        out.append({
            "app": "grok", "id": r["id"], "title": first_line(r.get("name")), **project,
            "preview": strip_md_links(entry.get("text") or "")[:240], "updatedAt": activity,
            "runningSince": 0, "status": status, "unreadCount": r.get("unreadCount") or 0,
        })
    out.sort(key=lambda s: s["updatedAt"], reverse=True)
    return out[:LIMIT_PER_APP]


# ---------------------------------------------------------------- WorkBuddy

# The overseas build keeps its data in ~/.workbuddy-ai, the China build in ~/.workbuddy.
WORKBUDDY_HOMES = [HOME / ".workbuddy-ai", HOME / ".workbuddy"]


def workbuddy_status(raw):
    """Same buckets as WorkBuddy's own normalizeTaskStatus."""
    s = re.sub(r"[\s-]+", "_", (raw or "").strip().lower())
    if s in ("planning", "preparing", "connecting", "working", "running", "tool_start", "tool_end", "handoff",
             "summarizing", "waiting_team_members", "model_requesting", "model_streaming", "model_done",
             "tool_executing"):
        return "working"
    if s in ("completed", "cancelled", "canceled", "done"):
        return "completed"
    if s in ("failed", "error", "terminated", "archived"):
        return s
    return "pending"  # pending, idle, await_input, waiting_input, … and anything unknown


def workbuddy_sessions():
    out = []
    for home in WORKBUDDY_HOMES:
        db = home / "workbuddy.db"
        if not db.exists():
            continue
        con = open_db(db)
        try:
            cols = columns(con, "sessions")
            if not {"id", "status"} <= cols:
                raise FormatError("sessions table has no id/status")
            activity = first_of(cols, "last_activity_at", "updated_at", "created_at", default="0")
            where = [w for c, w in (("deleted_at", "deleted_at is null"),) if c in cols]
            where.append("lower(status) != 'archived'")
            rows = con.execute(
                f"""select id, {first_of(cols, 'custom_title', 'title')}, {'cwd' if 'cwd' in cols else "''"}, status,
                           {activity}, {'is_playground' if 'is_playground' in cols else 0}
                    from sessions where {' and '.join(where)}
                    order by {activity} desc limit ?""",
                (LIMIT_PER_APP,),
            ).fetchall()
        finally:
            con.close()
        for sid, title, cwd, raw_status, activity_ms, playground in rows:
            activity_ms = activity_ms or 0
            status, running_since = "idle", 0
            state = workbuddy_status(raw_status)
            if state == "working" and now_ms() - activity_ms < RUNNING_STALE_SEC * 1000:
                status, running_since = "running", activity_ms
            elif state == "pending" and activity_ms:
                status = "waiting"  # a turn has happened and the task is waiting for your input
            elif state == "completed":
                status = finish("idle", activity_ms, 0, "workbuddy", sid)
            out.append({
                "app": "workbuddy", "id": sid, "title": first_line(title),
                # Playground tasks run in a generated timestamp folder that means nothing to the user.
                **folder_project("" if playground else cwd),
                "preview": "", "updatedAt": activity_ms, "runningSince": running_since, "status": status,
            })
    out.sort(key=lambda s: s["updatedAt"], reverse=True)
    return out[:LIMIT_PER_APP]


# ---------------------------------------------------------------- Cursor (cloud agents)

# Cursor caches the cloud agents shown in its Agents window in the global VS Code state database.
CURSOR_STATE = APP_SUPPORT / "Cursor" / "User" / "globalStorage" / "state.vscdb"
# aiserver.v1.BackgroundComposerStatus: 1 RUNNING, 2 FINISHED, 3 ERROR, 4 CREATING, 5 EXPIRED
CURSOR_RUNNING = (1, 4)


def cursor_sessions():
    if not CURSOR_STATE.exists():
        return []
    con = open_db(CURSOR_STATE)
    try:
        columns(con, "ItemTable")
        blobs = [v for (v,) in con.execute(
            "select value from ItemTable where key like 'cloudAgentRepository.agents.%'")]
    finally:
        con.close()

    out = []
    for blob in blobs:
        try:
            agents = json.loads(blob)
        except (TypeError, ValueError):
            continue
        for a in agents if isinstance(agents, list) else []:
            bc_id = a.get("bcId") if isinstance(a, dict) else None
            if not bc_id or a.get("isArchived") or a.get("isOwnedByDifferentTeamMember"):
                continue
            activity = a.get("lastMessageActivityAtMs") or a.get("updatedAt") or 0
            running_since = 0
            if a.get("hasPendingInteraction"):
                status = "waiting"
            elif a.get("status") in CURSOR_RUNNING and not a.get("isKilled"):
                status, running_since = "running", a.get("updatedAt") or activity
            elif a.get("isUnread"):
                status = finish("idle", activity, 0, "cursor", bc_id)
            else:
                status = finish("idle", activity, a.get("lastReadAtMs") or activity, "cursor", bc_id)
            repo = a.get("repoUrl") or ""
            name = repo.rstrip("/").split("/")[-1] if repo else ""
            out.append({
                "app": "cursor", "id": bc_id, "title": first_line(a.get("name")),
                "source": name, "sourceType": "folder" if name else "nofolder",
                "projectKey": f"repo:{repo}" if repo else "folder:none",
                "projectName": name, "projectKind": "folder" if name else "nofolder",
                "preview": "", "updatedAt": activity, "runningSince": running_since, "status": status,
            })
    out.sort(key=lambda s: s["updatedAt"], reverse=True)
    return out[:LIMIT_PER_APP]


# ---------------------------------------------------------------- the apps

# Everything Agent Hub knows about each app, in the order they're shown.
#   reader   returns this app's sessions (or raises FormatError)
#   id       ids that may be put into a link; anything else is refused
#   link     deep link to one conversation; {scheme} is the first of `schemes` the installed app registers
#   bundles  where to find the installed app: bundle ids (found anywhere via Spotlight) and .app names,
#            plus an icon file inside Resources when the bundle's own icon isn't the right one
#   letter, color   badge shown when the app's icon can't be read
APPS = {
    "codex": {
        "name": "Codex", "reader": codex_sessions, "id": r"[0-9a-f-]{36}",
        "link": "codex://threads/{id}",
        # Codex ships inside the ChatGPT desktop app; its own icon is app.icns there.
        "bundles": [{"ids": ["com.openai.codex"], "names": ["Codex.app", "ChatGPT.app"], "icon": "app.icns"}],
        "letter": "C", "color": "#0f8a6a",
    },
    "claude": {
        "name": "Claude", "reader": claude_sessions, "id": r"local_[A-Za-z0-9-]{1,64}",
        "link": "claude://code/continue?session={id}",
        "bundles": [{"ids": ["com.anthropic.claudefordesktop"], "names": ["Claude.app"]}],
        "letter": "A", "color": "#c4623f",
    },
    "grok": {
        "name": "Grok Bot", "reader": grok_sessions, "id": r"[A-Za-z0-9_-]{1,128}",
        "link": "grokbot://app/v1/agent?id={id}",
        "bundles": [{"ids": ["com.anysphere.sand"], "names": ["Grok Bot.app"]}],
        "letter": "G", "color": "#4f4fc9",
    },
    "workbuddy": {
        "name": "WorkBuddy", "reader": workbuddy_sessions, "id": r"[0-9a-f-]{36}",
        "link": "{scheme}://chat/{id}", "schemes": ["workbuddy-ai", "workbuddy"],
        "bundles": [{"ids": ["com.workbuddy.workbuddy-ai", "com.workbuddy.workbuddy"],
                     "names": ["WorkBuddy AI.app", "WorkBuddy.app"]}],
        "letter": "W", "color": "#2f6fe4",
    },
    "cursor": {
        "name": "Cursor", "reader": cursor_sessions, "id": r"bc-[0-9a-f-]{36}",
        "link": "cursor://anysphere.cursor-deeplink/background-agent?bcId={id}",
        "bundles": [{"ids": ["com.todesktop.230313mzl4w4u92"], "names": ["Cursor.app"]}],
        "letter": "Cu", "color": "#26251e",
    },
}


# ---------------------------------------------------------------- installed apps: location, icon, link scheme

_bundle_cache = {}


def find_bundle(app):
    """(path to the installed .app, icon file override) or (None, None)."""
    if app in _bundle_cache:
        return _bundle_cache[app]
    found = (None, None)
    for b in APPS[app]["bundles"]:
        for name in b.get("names", []):
            for root in (Path("/Applications"), HOME / "Applications", Path("/Applications/Setapp")):
                if (root / name / "Contents" / "Info.plist").is_file():
                    found = (root / name, b.get("icon"))
                    break
            if found[0]:
                break
        if not found[0]:
            for bid in b.get("ids", []):
                try:
                    hits = subprocess.run(["mdfind", f"kMDItemCFBundleIdentifier == '{bid}'"],
                                          capture_output=True, text=True, timeout=3).stdout.split("\n")
                except (OSError, subprocess.SubprocessError):
                    hits = []
                hit = next((h for h in hits if h.endswith(".app")), None)
                if hit:
                    found = (Path(hit), b.get("icon"))
                    break
        if found[0]:
            break
    _bundle_cache[app] = found
    return found


def bundle_info(path):
    try:
        with open(path / "Contents" / "Info.plist", "rb") as f:
            return plistlib.load(f)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return {}


def bundle_schemes(path):
    schemes = set()
    for t in bundle_info(path).get("CFBundleURLTypes") or []:
        schemes.update(s.lower() for s in t.get("CFBundleURLSchemes") or [])
    return schemes


def link_for(app, sid):
    spec = APPS[app]
    scheme = ""
    if "schemes" in spec:
        path, _ = find_bundle(app)
        registered = bundle_schemes(path) if path else set()
        scheme = next((s for s in spec["schemes"] if s in registered), spec["schemes"][0])
    return spec["link"].format(id=sid, scheme=scheme)


def find_icns(app):
    path, icon = find_bundle(app)
    if not path:
        return None
    resources = path / "Contents" / "Resources"
    candidates = [icon] if icon else []
    own = bundle_info(path).get("CFBundleIconFile") or ""
    if own:
        candidates.append(own if own.endswith(".icns") else own + ".icns")
    return next((resources / c for c in candidates if (resources / c).is_file()), None)


def app_icon_png(app):
    """PNG of the app's icon, converted once with macOS's sips and cached."""
    icns = find_icns(app)
    if not icns:
        return None
    out = ICON_CACHE / f"{app}.png"
    try:
        if not out.exists() or out.stat().st_mtime < icns.stat().st_mtime:
            ICON_CACHE.mkdir(parents=True, exist_ok=True)
            subprocess.run(["sips", "-s", "format", "png", "-Z", "64", str(icns), "--out", str(out)],
                           check=True, capture_output=True, timeout=10)
        return out.read_bytes()
    except (OSError, subprocess.SubprocessError):
        return None


# ---------------------------------------------------------------- demo data

def demo_sessions(lang):
    minute = 60 * 1000
    t = now_ms()
    en = lang == "en"
    content_team = "Content team" if en else "内容运营小组"
    app_team = "App team" if en else "App 开发小组"
    rows = [
        # app, (zh title, en title), project, kind, status, minutes ago, (zh preview, en preview)
        ("grok", ("社媒文案助手", "Social copywriter"), content_team, "group", "waiting", 12,
         ("这篇的语气对你的账号来说合适吗？要不要我再改得口语一点？", "Does this tone fit your account? Want me to make it more casual?")),
        ("claude", ("重构登录流程", "Refactor the sign-in flow"), "acme-web", "folder", "waiting", 4,
         ("迁移脚本会删除旧的 session 表，确认后我再执行。", "The migration drops the old sessions table. Confirm and I'll run it.")),
        ("claude", ("修复深色模式下的对比度问题", "Fix contrast issues in dark mode"), "acme-web", "folder", "unseen", 9, None),
        ("codex", ("为支付模块补充单元测试", "Add unit tests for payments"), "acme-api", "folder", "unseen", 26, None),
        ("grok", ("竞品调研", "Competitor research"), None, "bot", "unseen", 55, None),
        ("codex", ("升级依赖并修复构建", "Upgrade dependencies and fix the build"), "acme-ios", "folder", "running", 7, None),
        ("claude", ("设计系统组件文档", "Document design system components"), "design-system", "folder", "running", 3, None),
        ("codex", ("整理 TestFlight 发布步骤", "Write up TestFlight release steps"), "acme-ios", "folder", "idle", 70, None),
        ("grok", ("周报汇总", "Weekly report"), content_team, "group", "idle", 180, None),
        ("codex", ("优化列表滚动性能", "Speed up list scrolling"), "acme-web", "folder", "idle", 240, None),
        ("claude", ("API 错误码梳理", "Tidy up API error codes"), "acme-api", "folder", "idle", 420, None),
        ("grok", ("产品经理", "Product manager"), app_team, "group", "idle", 900, None),
        ("codex", ("首页加载速度分析", "Profile home page load time"), "acme-web", "folder", "idle", 1500, None),
        ("grok", ("UI 设计师", "UI designer"), app_team, "group", "idle", 2000, None),
        ("workbuddy", ("整理本周会议纪要", "Summarize this week's meetings"), None, "nofolder", "waiting", 18,
         ("找到 5 份纪要，要按项目分组还是按日期？", "Found 5 sets of notes. Group them by project or by date?")),
        ("cursor", ("迁移到新的路由方案", "Migrate to the new router"), "acme-web", "folder", "running", 14, None),
        ("cursor", ("修复 CI 里不稳定的测试", "Fix flaky tests in CI"), "acme-api", "folder", "unseen", 40, None),
        ("workbuddy", ("生成季度汇报 PPT", "Build the quarterly review deck"), "reports", "folder", "running", 5, None),
        ("workbuddy", ("对比三家供应商报价", "Compare three vendor quotes"), "reports", "folder", "idle", 300, None),
    ]
    out = []
    for i, (app, title, project, kind, status, ago, preview) in enumerate(rows):
        out.append({
            "app": app, "id": f"demo-{i}", "title": title[en],
            "source": project or "", "sourceType": kind,
            "projectKey": f"{kind}:{project}", "projectName": project or "",
            "projectKind": {"group": "group", "bot": "bots", "nofolder": "nofolder"}.get(kind, "folder"),
            "preview": preview[en] if preview else "", "updatedAt": t - ago * minute,
            "runningSince": t - ago * minute if status == "running" else 0,
            "status": status,
        })
    return out


# ---------------------------------------------------------------- server

def apps_meta():
    return {k: {"name": a["name"], "letter": a["letter"], "color": a["color"]} for k, a in APPS.items()}


def collect(lang="zh"):
    if DEMO:
        return {"sessions": demo_sessions(lang), "errors": {}, "apps": apps_meta(), "generatedAt": now_ms(), "demo": True}
    sessions, errors = [], {}
    for key, spec in APPS.items():
        try:
            sessions.extend(spec["reader"]())
        except FormatError as e:
            errors[key] = f"unrecognized data format ({e}); the app may have updated"
        except Exception as e:  # one broken source shouldn't blank the page
            errors[key] = f"{type(e).__name__}: {e}"
    return {"sessions": sessions, "errors": errors, "apps": apps_meta(), "generatedAt": now_ms()}


ICON_PATH = re.compile(r"/icon/([a-z]+)\.png")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        icon = ICON_PATH.fullmatch(path)
        if path in ("/", "/index.html"):
            self.send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        elif icon and icon.group(1) in APPS:
            png = app_icon_png(icon.group(1))
            self.send(200, png, "image/png") if png else self.send(404, {"error": "no icon"})
        elif path == "/api/sessions":
            # The page passes its UI language; only demo data is localized server-side.
            self.send(200, collect("en" if "lang=en" in self.path else "zh"))
        elif path == "/api/ping":
            self.send(200, {"app": "agent-hub", "demo": DEMO})
        else:
            self.send(404, {"error": "not found"})

    def do_POST(self):
        if self.path not in ("/api/open", "/api/seen"):
            return self.send(404, {"error": "not found"})
        # Only accept same-origin requests so other web pages can't drive this.
        origin = self.headers.get("Origin", "")
        if origin and origin not in (f"http://{HOST}:{PORT}", f"http://localhost:{PORT}"):
            return self.send(403, {"error": "bad origin"})
        if DEMO:  # nothing to open or remember for made-up conversations
            return self.send(200, {"ok": True, "demo": True})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            app, sid = body["app"], str(body["id"])
            spec = APPS[app]
        except (ValueError, KeyError, TypeError):
            return self.send(400, {"error": "bad request"})
        if not re.fullmatch(spec["id"], sid):
            return self.send(400, {"error": "bad id"})
        STATE.mark(app, sid)
        if self.path == "/api/seen":
            return self.send(200, {"ok": True})
        url = link_for(app, sid)
        subprocess.run(["open", url], check=False)
        self.send(200, {"ok": True, "url": url})


def running_hub(port):
    """True if an Agent Hub is already answering on this port."""
    try:
        with urllib.request.urlopen(f"http://{HOST}:{port}/api/ping", timeout=1) as r:
            return json.load(r).get("app") == "agent-hub"
    except (OSError, ValueError):
        return False


def bind():
    """Bind the default port, or the next free one. Returns (server, port), or (None, port) when a hub is already running there."""
    for port in range(DEFAULT_PORT, DEFAULT_PORT + 20):
        try:
            return ThreadingHTTPServer((HOST, port), Handler), port
        except OSError:
            if port == DEFAULT_PORT and running_hub(port):
                return None, port
    sys.exit(f"No free port between {DEFAULT_PORT} and {DEFAULT_PORT + 19}. Set AGENT_HUB_PORT to another number.")


# ---------------------------------------------------------------- --check

def check():
    """Plain-text report of what was found, meant to be pasted into a GitHub issue."""
    print(f"Agent Hub check · macOS {platform.mac_ver()[0] or '?'} ({platform.machine()}) · Python {platform.python_version()}")
    counts = {}
    for key, spec in APPS.items():
        path, _ = find_bundle(key)
        info = bundle_info(path) if path else {}
        version = info.get("CFBundleShortVersionString", "?")
        print(f"\n{spec['name']}")
        print(f"  app:     {f'{path} (version {version})' if path else 'not found'}")
        if path:
            scheme = link_for(key, "x").split("://")[0]
            ok = scheme in bundle_schemes(path)
            print(f"  link:    {scheme}:// {'registered' if ok else 'NOT registered by this app'}")
            print(f"  icon:    {'ok' if find_icns(key) else 'not found (letter badge will be shown)'}")
        try:
            sessions = spec["reader"]()
        except FormatError as e:
            print(f"  data:    UNRECOGNIZED FORMAT: {e}")
            continue
        except Exception as e:
            print(f"  data:    ERROR {type(e).__name__}: {e}")
            continue
        by_status = {}
        for s in sessions:
            by_status[s["status"]] = by_status.get(s["status"], 0) + 1
        counts[key] = len(sessions)
        detail = ", ".join(f"{n} {st}" for st, n in sorted(by_status.items()))
        print(f"  data:    {len(sessions)} conversations" + (f" ({detail})" if detail else ""))
    if not any(counts.values()):
        print("\nNo conversations found. Start one in a supported app, or try: python3 agent_hub.py --demo")


def main():
    global PORT
    if "--check" in sys.argv:
        return check()
    if sys.platform != "darwin" and not DEMO:
        print("Agent Hub reads macOS apps' local data; on this system only --demo shows anything.")
    server, PORT = bind()
    url = f"http://{HOST}:{PORT}/"
    if server is None:
        print(f"Agent Hub is already running: {url}", flush=True)
        if "--no-open" not in sys.argv:
            webbrowser.open(url)
        return
    print(f"Agent Hub{' (demo)' if DEMO else ''}: {url}  (Ctrl+C to quit)", flush=True)
    if "--no-open" not in sys.argv:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
