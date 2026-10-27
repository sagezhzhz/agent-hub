#!/usr/bin/env python3
"""Agent Hub: one local page listing Codex / Claude / Grok Bot desktop conversations.

Reads each app's local data read-only, serves a dashboard on 127.0.0.1,
and jumps back into the right app via its URL scheme when a card is clicked.
The only file it writes is agent_hub_state.json next to this script, which
remembers which finished conversations you have already looked at.
Python 3 standard library only.
"""

import base64
import glob
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST, PORT = "127.0.0.1", int(os.environ.get("AGENT_HUB_PORT", "47321"))
HOME = Path.home()
APP_SUPPORT = HOME / "Library" / "Application Support"
PAGE = Path(__file__).with_name("agent_hub.html")
STATE_FILE = Path(__file__).with_name("agent_hub_state.json")

LIMIT_PER_APP = 80
RUNNING_STALE_SEC = 30 * 60   # a "started" turn older than this is treated as stale, not running
FIRST_RUN_LOOKBACK_SEC = 6 * 3600  # on first launch, only the last few hours count as "unseen"


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
        if "baselineAt" not in data:
            self._save()

    def seen_at(self, app, sid):
        return max(self.seen.get(f"{app}:{sid}", 0), self.baseline)

    def mark(self, app, sid):
        with self.lock:
            self.seen[f"{app}:{sid}"] = now_ms()
            self._save()

    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"baselineAt": self.baseline, "seen": self.seen}))
        tmp.replace(self.path)


STATE = SeenState(STATE_FILE)


def finish(status, updated_at, seen_after, app, sid):
    """Turn an idle conversation into "unseen" when it changed after you last looked."""
    if status == "idle" and updated_at > max(seen_after, STATE.seen_at(app, sid)):
        return "unseen"
    return status


# ---------------------------------------------------------------- Codex

CODEX_DB = HOME / ".codex" / "state_5.sqlite"
CODEX_HIDDEN_SOURCES = ("guardian_review", "subagent")
TURN_EVENT = re.compile(r'"timestamp":"([^"]+)".*"payload":\{"type":"(task_started|task_complete|turn_aborted)"')


def iso_ms(ts):
    try:
        return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return 0


def codex_turn_state(rollout_path):
    """Last turn lifecycle event in the rollout file and when it happened."""
    try:
        with open(rollout_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 256 * 1024))
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        return None, 0
    for line in reversed(tail.splitlines()):
        if '"event_msg"' not in line:
            continue
        m = TURN_EVENT.search(line)
        if m:
            return m.group(2), iso_ms(m.group(1))
    return None, 0


def codex_sessions():
    if not CODEX_DB.exists():
        return []
    con = sqlite3.connect(f"file:{CODEX_DB}?mode=ro", uri=True, timeout=2)
    try:
        rows = con.execute(
            f"""select id, coalesce(nullif(name,''), nullif(title,''), first_user_message), cwd,
                       coalesce(updated_at_ms, updated_at*1000), rollout_path, preview
                from threads
                where archived = 0 and coalesce(thread_source,'') not in ({",".join("?" * len(CODEX_HIDDEN_SOURCES))})
                order by coalesce(nullif(recency_at_ms,0), updated_at_ms, updated_at*1000) desc
                limit ?""",
            (*CODEX_HIDDEN_SOURCES, LIMIT_PER_APP),
        ).fetchall()
    finally:
        con.close()

    out = []
    for tid, title, cwd, updated, rollout, preview in rows:
        state, state_at = codex_turn_state(rollout)
        running_since = 0
        if state == "task_started" and now_ms() - updated < RUNNING_STALE_SEC * 1000:
            status, running_since = "running", state_at or updated
        else:
            status = "idle"
            if state == "task_complete":
                status = finish("idle", state_at or updated, 0, "codex", tid)
        out.append({
            "app": "codex",
            "id": tid,
            "title": (title or "未命名对话").strip().splitlines()[0][:120],
            **folder_project(cwd),
            "preview": (preview or "").strip()[:240],
            "updatedAt": updated,
            "runningSince": running_since,
            "status": status,
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
        if d.get("isArchived"):
            continue
        sid = d.get("sessionId", "")
        waiting_dir = Path(path).parent / "waiting-input"
        user_at = d.get("latestUserFrameAt") or 0
        edge_at = d.get("lastTurnEdgeAt") or 0
        summary = d.get("postTurnSummary") or {}
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
            "app": "claude",
            "id": sid,
            "title": d.get("title") or "未命名会话",
            **folder_project(d.get("cwd", "")),
            "preview": summary_text(summary),
            "updatedAt": max(d.get("lastActivityAt") or 0, user_at, edge_at),
            "runningSince": running_since,
            "status": status,
        })
    out.sort(key=lambda s: s["updatedAt"], reverse=True)
    return out[:LIMIT_PER_APP]


def summary_text(summary):
    if not isinstance(summary, dict):
        return ""
    return (summary.get("needs_action") or summary.get("status_detail") or "")[:240]


# ---------------------------------------------------------------- Grok Bot

GROK_STORE = APP_SUPPORT / "Grok Bot" / "sand-client-persistence"


def b32key(name):
    s = name.upper()
    return base64.b32decode(s + "=" * (-len(s) % 8)).decode()


def grok_read(suffix):
    """Find the persisted slice whose decoded key ends with `suffix` (any signed-in account)."""
    if not GROK_STORE.is_dir():
        return None
    for f in GROK_STORE.glob("*.blob"):
        try:
            key = b32key(f.stem)
        except ValueError:
            continue
        if key.startswith("sand.client.slice.account.") and key.endswith(suffix):
            try:
                return json.loads(f.read_text()).get("value")
            except (OSError, ValueError):
                return None
    return None


def grok_sessions():
    roster = (grok_read(".roster.last-roster") or {}).get("rows", [])
    # A bot that belongs to a group is filed under that group's project.
    parent = {}
    for r in roster:
        if r.get("isGroup"):
            for m in r.get("memberIds") or []:
                parent.setdefault(m, r)
    out = []
    for r in roster:
        if r.get("isHiddenFromSidebar"):
            continue
        entry = r.get("lastEntry") or {}
        activity = max(r.get("lastActivityAt") or 0, r.get("updatedAt") or 0)
        if r.get("awaitingUserResponse"):
            status = "waiting"
        elif r.get("hasUnread"):
            status = finish("idle", activity, 0, "grok", r["id"])
        else:
            status = finish("idle", r.get("lastActivityAt") or 0, r.get("lastViewedAt") or 0, "grok", r["id"])
        if r.get("isGroup"):
            members = len(r.get("memberIds") or [])
            project = {"source": f"群组 · {members} 个成员", "projectKey": f"grok:{r['id']}",
                       "projectName": r.get("name") or "群组", "projectKind": "group"}
        elif r["id"] in parent:
            g = parent[r["id"]]
            project = {"source": g.get("name") or "群组", "projectKey": f"grok:{g['id']}",
                       "projectName": g.get("name") or "群组", "projectKind": "group"}
        else:
            project = {"source": "Bot", "projectKey": "grok:bots", "projectName": "Grok Bot", "projectKind": "group"}
        out.append({
            "app": "grok",
            "id": r["id"],
            "title": r.get("name") or "未命名 Bot",
            **project,
            "preview": strip_md_links(entry.get("text") or "")[:240],
            "updatedAt": activity,
            "runningSince": 0,
            "status": status,
            "unreadCount": r.get("unreadCount") or 0,
        })
    out.sort(key=lambda s: s["updatedAt"], reverse=True)
    return out[:LIMIT_PER_APP]


def strip_md_links(text):
    return re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)


# ---------------------------------------------------------------- shared

def folder_project(cwd):
    if not cwd or "/scratch-workspaces/" in cwd:
        return {"source": "无文件夹", "projectKey": "folder:none", "projectName": "无文件夹", "projectKind": "folder"}
    name = Path(cwd).name or cwd
    return {"source": name, "projectKey": f"folder:{cwd}", "projectName": name, "projectKind": "folder"}


SOURCES = {"codex": codex_sessions, "claude": claude_sessions, "grok": grok_sessions}

# --demo serves made-up conversations instead of reading the apps: for screenshots
# and for trying the dashboard on a Mac without any of the apps installed.
DEMO = "--demo" in sys.argv


def demo_sessions():
    minute = 60 * 1000
    t = now_ms()
    rows = [
        # app, title, project name, project kind, status, minutes ago, preview
        ("grok", "社媒文案助手", "内容运营小组", "group", "waiting", 12, "这篇的语气对你的账号来说合适吗？要不要我再改得口语一点？"),
        ("claude", "重构登录流程", "acme-web", "folder", "waiting", 4, "迁移脚本会删除旧的 session 表，确认后我再执行。"),
        ("claude", "修复深色模式下的对比度问题", "acme-web", "folder", "unseen", 9, ""),
        ("codex", "为支付模块补充单元测试", "acme-api", "folder", "unseen", 26, ""),
        ("grok", "竞品调研", "Bot", "bot", "unseen", 55, ""),
        ("codex", "升级依赖并修复构建", "acme-ios", "folder", "running", 7, ""),
        ("claude", "设计系统组件文档", "design-system", "folder", "running", 3, ""),
        ("codex", "整理 TestFlight 发布步骤", "acme-ios", "folder", "idle", 70, ""),
        ("grok", "周报汇总", "内容运营小组", "group", "idle", 180, ""),
        ("codex", "优化列表滚动性能", "acme-web", "folder", "idle", 240, ""),
        ("claude", "API 错误码梳理", "acme-api", "folder", "idle", 420, ""),
        ("grok", "产品经理", "App 开发小组", "group", "idle", 900, ""),
        ("codex", "首页加载速度分析", "acme-web", "folder", "idle", 1500, ""),
        ("grok", "UI 设计师", "App 开发小组", "group", "idle", 2000, ""),
    ]
    out = []
    for i, (app, title, project, kind, status, ago, preview) in enumerate(rows):
        source = project if kind != "bot" else "Bot"
        out.append({
            "app": app, "id": f"demo-{i}", "title": title,
            "source": source, "projectKey": f"{kind}:{project}", "projectName": "Grok Bot" if kind == "bot" else project,
            "projectKind": "group" if kind in ("group", "bot") else "folder",
            "preview": preview, "updatedAt": t - ago * minute,
            "runningSince": t - ago * minute if status == "running" else 0,
            "status": status,
        })
    return out

# Deep links are built here from validated ids only; the page never supplies a URL.
DEEP_LINKS = {
    "codex": (re.compile(r"^[0-9a-f-]{36}$"), "codex://threads/{id}"),
    "claude": (re.compile(r"^local_[A-Za-z0-9-]{1,64}$"), "claude://code/continue?session={id}"),
    "grok": (re.compile(r"^[A-Za-z0-9_-]{1,128}$"), "grokbot://app/v1/agent?id={id}"),
}


def collect():
    if DEMO:
        return {"sessions": demo_sessions(), "errors": {}, "generatedAt": now_ms(), "demo": True}
    sessions, errors = [], {}
    for name, fn in SOURCES.items():
        try:
            sessions.extend(fn())
        except Exception as e:  # one broken source shouldn't blank the page
            errors[name] = f"{type(e).__name__}: {e}"
    return {"sessions": sessions, "errors": errors, "generatedAt": now_ms()}


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
        if self.path in ("/", "/index.html"):
            self.send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/api/sessions":
            self.send(200, collect())
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
            app = body["app"]
            pattern, template = DEEP_LINKS[app]
            sid = str(body["id"])
        except (ValueError, KeyError, TypeError):
            return self.send(400, {"error": "bad request"})
        if not pattern.match(sid):
            return self.send(400, {"error": "bad id"})
        STATE.mark(app, sid)
        if self.path == "/api/seen":
            return self.send(200, {"ok": True})
        url = template.format(id=sid)
        subprocess.run(["open", url], check=False)
        self.send(200, {"ok": True, "url": url})


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}/"
    print(f"Agent Hub 已启动{'（演示模式）' if DEMO else ''}：{url}  （Ctrl+C 退出）")
    if "--no-open" not in sys.argv:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
