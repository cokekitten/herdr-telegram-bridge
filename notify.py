#!/usr/bin/env python3
"""herdr plugin hook: push a Telegram message on agent status changes.

Invoked by herdr with the event payload in HERDR_PLUGIN_EVENT_JSON.
Config lives in <plugin config dir>/config.toml (see config.example.toml).
"""

import glob
import json
import os
import subprocess
import sys
import time
import tomllib
import urllib.parse
import urllib.request

STATE_DIR = os.environ.get("HERDR_PLUGIN_STATE_DIR") or os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.environ.get("HERDR_PLUGIN_CONFIG_DIR") or os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(STATE_DIR, "notify.log")

STATUS_EMOJI = {"done": "✅", "blocked": "❓", "idle": "💤", "working": "⏳", "unknown": "❔"}


def agent_info(pane_id: str) -> dict:
    """Enrich via `herdr agent get`: cwd, terminal title, session ref."""
    herdr = os.environ.get("HERDR_BIN_PATH", "herdr")
    try:
        out = subprocess.run([herdr, "agent", "get", pane_id], capture_output=True, timeout=10, check=False)
        return json.loads(out.stdout.decode()).get("result", {}).get("agent", {})
    except Exception as e:
        log(f"agent get failed for {pane_id}: {e!r}")
        return {}


def session_file(agent: str, session: dict) -> str:
    """Resolve the agent's transcript path from its herdr session ref."""
    kind, value = session.get("kind"), session.get("value", "")
    if kind == "path":
        if os.path.isfile(value):
            return value
        # stale ref (session rolled over): newest jsonl in the same dir
        d = os.path.dirname(value)
        hits = glob.glob(os.path.join(d, "*.jsonl")) if os.path.isdir(d) else []
        return max(hits, key=os.path.getmtime) if hits else ""
    if kind == "id" and value:
        home = os.path.expanduser("~")
        patterns = {
            "claude": f"{home}/.claude/projects/*/{value}.jsonl",
            "codex": f"{home}/.codex/sessions/*/*/*/rollout-*{value}.jsonl",
        }
        pat = patterns.get(agent)
        if pat:
            hits = glob.glob(pat)
            if hits:
                return max(hits, key=os.path.getmtime)
    return ""


def _texts_from_content(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            c["text"] for c in content
            if isinstance(c, dict) and c.get("type") in ("text", "output_text") and c.get("text")
        )
    return ""


def _tail_lines(path: str, max_bytes: int = 2_000_000) -> list:
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - max_bytes))
            return f.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []


def last_reply(path: str) -> str:
    """Last assistant text in a session jsonl (claude / codex / pi / generic shapes)."""
    for ln in reversed(_tail_lines(path)):
        try:
            o = json.loads(ln)
        except Exception:
            continue
        # claude: type=assistant + message.role; pi: type=message + message.role;
        # codex: payload.role; generic: top-level role
        for holder in (o.get("message"), o.get("payload"), o):
            if isinstance(holder, dict) and holder.get("role") == "assistant":
                t = _texts_from_content(holder.get("content")).strip()
                if t:
                    return t
    return ""


def last_reply_grok(cwd: str) -> str:
    """grok stores per-cwd sessions: ~/.grok/sessions/<urlencoded-cwd>/<id>/chat_history.jsonl"""
    if not cwd:
        return ""
    base = os.path.join(os.path.expanduser("~/.grok/sessions"), urllib.parse.quote(cwd, safe=""))
    dirs = [d for d in glob.glob(os.path.join(base, "*")) if os.path.isdir(d)]
    if not dirs:
        return ""
    hist = os.path.join(max(dirs, key=os.path.getmtime), "chat_history.jsonl")
    for ln in reversed(_tail_lines(hist)):
        try:
            o = json.loads(ln)
        except Exception:
            continue
        if o.get("type") == "assistant":
            t = o.get("content")
            t = (t if isinstance(t, str) else _texts_from_content(t)).strip()
            if t:
                return t
    return ""


def last_reply_kimi(session_id: str) -> str:
    """kimi-code: session_index.jsonl maps id -> dir; text lives in wire.jsonl content.part events."""
    idx = os.path.expanduser("~/.kimi-code/session_index.jsonl")
    sdir = ""
    for ln in _tail_lines(idx):
        try:
            o = json.loads(ln)
        except Exception:
            continue
        if o.get("sessionId") == session_id:
            sdir = o.get("sessionDir", "")
    if not sdir:
        return ""
    wires = sorted(glob.glob(os.path.join(sdir, "agents", "*", "wire.jsonl")))
    main_wire = os.path.join(sdir, "agents", "main", "wire.jsonl")
    wire = main_wire if main_wire in wires else (wires[0] if wires else "")
    for ln in reversed(_tail_lines(wire)):
        try:
            o = json.loads(ln)
        except Exception:
            continue
        e = o.get("event", {})
        if o.get("type") == "context.append_loop_event" and e.get("type") == "content.part":
            part = e.get("part", {})
            if part.get("type") == "text" and part.get("text", "").strip():
                return part["text"].strip()
    return ""


def last_reply_hermes(session_id: str) -> str:
    """hermes keeps transcripts in sqlite: ~/.hermes/state.db messages table."""
    import sqlite3

    db_path = os.path.expanduser("~/.hermes/state.db")
    if not os.path.isfile(db_path):
        return ""
    try:
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3)
        row = db.execute(
            "select content from messages where session_id=? and role='assistant'"
            " and content is not null and content != '' order by id desc limit 1",
            (session_id,),
        ).fetchone()
        db.close()
        return (row[0] if row else "").strip()
    except Exception as e:
        log(f"hermes db read failed: {e!r}")
        return ""


def last_reply_opencode(session_id: str) -> str:
    """opencode: sqlite ~/.local/share/opencode/opencode.db, message + part tables."""
    import sqlite3

    db_path = os.path.expanduser("~/.local/share/opencode/opencode.db")
    if not os.path.isfile(db_path):
        return ""
    try:
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3)
        msgs = db.execute(
            "select id, data from message where session_id=? order by time_created desc limit 20",
            (session_id,),
        ).fetchall()
        for mid, data in msgs:
            try:
                if json.loads(data).get("role") != "assistant":
                    continue
            except Exception:
                continue
            parts = db.execute(
                "select data from part where message_id=? order by time_created", (mid,)
            ).fetchall()
            texts = []
            for (pd,) in parts:
                try:
                    pj = json.loads(pd)
                except Exception:
                    continue
                if pj.get("type") == "text" and pj.get("text", "").strip():
                    texts.append(pj["text"].strip())
            if texts:
                db.close()
                return "\n".join(texts)
        db.close()
    except Exception as e:
        log(f"opencode db read failed: {e!r}")
    return ""


def reply_snippet(agent: str, session: dict, cwd: str) -> str:
    if agent == "grok":
        return last_reply_grok(cwd)
    if agent == "kimi":
        return last_reply_kimi(session.get("value", ""))
    if agent == "hermes":
        return last_reply_hermes(session.get("value", ""))
    if agent == "opencode":
        return last_reply_opencode(session.get("value", ""))
    sf = session_file(agent, session)
    return last_reply(sf) if sf else ""


def log(msg: str) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")


def load_config() -> dict | None:
    path = os.path.join(CONFIG_DIR, "config.toml")
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        log(f"no config at {path}; skipping")
        return None
    except tomllib.TOMLDecodeError as e:
        log(f"bad config {path}: {e}")
        return None


def send_telegram(cfg: dict, text: str) -> None:
    token, chat_id = cfg.get("bot_token", ""), str(cfg.get("chat_id", ""))
    if not token or not chat_id or "REPLACE" in token or "REPLACE" in chat_id:
        log("bot_token/chat_id not set; skipping send")
        return
    base = cfg.get("api_base", "https://api.telegram.org").rstrip("/")
    url = f"{base}/bot{token}/sendMessage"
    # curl first: empirically the only reliably working TLS path on this box
    # (python/OpenSSL handshakes to api.telegram.org get reset intermittently).
    cmd = ["curl", "-sm", "15", url, "-d", f"chat_id={chat_id}", "--data-urlencode", f"text={text}"]
    proxy = cfg.get("proxy", "")
    if proxy:
        cmd[1:1] = ["-x", proxy]
    last_err = None
    for attempt in range(3):
        try:
            out = subprocess.run(cmd, capture_output=True, timeout=20, check=False)
            body = json.loads(out.stdout.decode() or "{}")
            if body.get("ok"):
                log(f"sent ok: {text!r}")
                return
            last_err = f"curl exit={out.returncode} body={out.stdout[:200]!r}"
        except Exception as e:
            last_err = repr(e)
        time.sleep(1 + attempt)
    # fallback: urllib with system proxy
    try:
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=15) as resp:
            body = json.loads(resp.read().decode())
            log(f"sent ok={body.get('ok')} (urllib fallback): {text!r}")
            return
    except Exception as e:
        last_err = f"{last_err}; urllib: {e!r}"
    log(f"send failed: {last_err}: {text!r}")


def swap_last_status(pane_id: str, status: str) -> str:
    """Record this pane's status and return the previous one ("" if unseen)."""
    path = os.path.join(STATE_DIR, "last_status.json")
    try:
        with open(path, encoding="utf-8") as f:
            seen = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        seen = {}
    prev = seen.get(pane_id, "")
    if len(seen) > 500:
        seen = {}
    seen[pane_id] = status
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(seen, f)
    return prev


def recently_sent(key: str, quiet_seconds: int) -> bool:
    """Debounce: skip if the same pane+status fired within quiet_seconds."""
    if quiet_seconds <= 0:
        return False
    path = os.path.join(STATE_DIR, "last_sent.json")
    now = time.time()
    try:
        with open(path, encoding="utf-8") as f:
            seen = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        seen = {}
    last = seen.get(key, 0)
    seen = {k: v for k, v in seen.items() if now - v < 3600}
    seen[key] = now
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(seen, f)
    return now - last < quiet_seconds


def main() -> None:
    cfg = load_config()

    if "--test" in sys.argv:
        if cfg is None:
            print(f"config.toml not found in {CONFIG_DIR}", file=sys.stderr)
            sys.exit(1)
        send_telegram(cfg, "tg.notify test message from herdr 🔔")
        return

    raw = os.environ.get("HERDR_PLUGIN_EVENT_JSON", "")
    if not raw:
        log("no HERDR_PLUGIN_EVENT_JSON; exiting")
        return
    try:
        event = json.loads(raw)
    except json.JSONDecodeError as e:
        log(f"bad event json: {e}")
        return
    # payload shape: {"event": "pane_agent_status_changed", "data": {...fields...}}
    if isinstance(event.get("data"), dict):
        event = event["data"]
    if os.environ.get("TG_NOTIFY_DEBUG") or "agent_status" not in event:
        log(f"raw event: {raw[:500]}")

    status = event.get("agent_status", "unknown")
    pane_id = event.get("pane_id", "?")
    agent = event.get("display_agent") or event.get("agent") or "agent"
    title = event.get("title") or ""
    log(f"event: pane={pane_id} agent={agent} status={status}")

    if cfg is None:
        return
    # herdr only reports "done" for unfocused panes (attention semantics); a
    # focused pane goes working->idle directly. Treat that as done too.
    prev = swap_last_status(pane_id, status)
    if status == "idle" and prev == "working":
        status = "done"
        log(f"working->idle on {pane_id}: treating as done (focused completion)")
    if status not in cfg.get("statuses", ["done"]):
        return
    agents = cfg.get("agents", [])
    if agents and (event.get("agent") or "") not in agents:
        return
    if recently_sent(f"{pane_id}:{status}", int(cfg.get("quiet_seconds", 5))):
        log(f"debounced pane={pane_id} status={status}")
        return

    info = agent_info(pane_id)
    cwd = info.get("cwd") or info.get("foreground_cwd") or ""
    folder = cwd.replace(os.path.expanduser("~"), "~") if cwd else ""
    title = info.get("terminal_title_stripped") or title
    snippet = " ".join(reply_snippet(event.get("agent") or "", info.get("agent_session") or {}, cwd).split())
    limit = int(cfg.get("snippet_chars", 150))
    if len(snippet) > limit:
        snippet = snippet[:limit] + "…"

    emoji = STATUS_EMOJI.get(status, "")
    lines = [f"{emoji} {agent} {status} · {folder or pane_id}"]
    if title:
        lines.append(f"📝 {title}")
    if snippet:
        lines.append(f"💬 {snippet}")
    send_telegram(cfg, "\n".join(lines))


if __name__ == "__main__":
    main()
