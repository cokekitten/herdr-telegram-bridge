#!/usr/bin/env python3
"""herdr plugin hook: push a Telegram message on agent status changes.

Invoked by herdr with the event payload in HERDR_PLUGIN_EVENT_JSON.
Config lives in <plugin config dir>/config.toml (see config.example.toml).

Every notification is recorded against the agent it describes, so replying to it in
Telegram routes back to that pane — see bot.py, the poller this hook keeps alive.
"""

import glob
import json
import os
import sys
import time
import urllib.parse

import tg


def agent_info(pane_id: str) -> dict:
    """Enrich via `herdr agent get`: cwd, terminal title, session ref."""
    result, err = tg.herdr_json("agent", "get", pane_id, timeout=10)
    if err:
        tg.log(f"agent get failed for {pane_id}: {err}")
        return {}
    return result.get("agent", {})


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
        tg.log(f"hermes db read failed: {e!r}")
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
        tg.log(f"opencode db read failed: {e!r}")
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


CHUNK_CHARS = 3600  # Telegram caps a message at 4096; leave room for the header
FOLD_OVER = 280     # shorter replies read better inline than behind an expander


def send_message(cfg: dict, text: str, plain: str | None = None, target: dict | None = None) -> None:
    """Send one message. `plain` enables HTML rendering with itself as the fallback, so
    a bug in the markdown renderer degrades to unformatted text instead of losing the
    notification entirely."""
    chat = tg.chat_id(cfg)
    if not chat:
        tg.log("chat_id not set; skipping send")
        return
    params = {"chat_id": chat, "text": text, "link_preview_options": '{"is_disabled":true}'}
    if plain is not None:
        params["parse_mode"] = "HTML"
    result, err = tg.api(cfg, "sendMessage", params)
    if err and plain is not None and ("parse" in err.lower() or "entit" in err.lower()):
        tg.log(f"HTML rejected ({err[-160:]}); resending as plain text")
        params.pop("parse_mode")
        params["text"] = plain[:4000]
        result, err = tg.api(cfg, "sendMessage", params)
    if err:
        tg.log(f"send failed: {err}: {text[:120]!r}")
        return
    tg.log(f"sent ok ({len(text)} chars)")
    if target:
        tg.remember_target((result or {}).get("message_id"), target,
                           int(cfg.get("msg_map_limit", tg.MSG_MAP_LIMIT)))


def tidy(text: str) -> str:
    """Trim trailing spaces and collapse blank-line runs, keeping the markdown shape."""
    out: list[str] = []
    for line in text.strip().split("\n"):
        line = line.rstrip()
        if not line and out and not out[-1]:
            continue
        out.append(line)
    return "\n".join(out)


def send_notification(cfg: dict, header: str, reply: str, target: dict) -> None:
    """One notification, split across as many messages as the reply needs. Every chunk
    is mapped back to the same agent, so replying to any of them routes correctly."""
    limit = int(cfg.get("snippet_chars", 0))
    if limit > 0 and len(reply) > limit:
        reply = reply[:limit].rstrip() + "…"

    if not cfg.get("markdown", True):
        flat = " ".join(reply.split())
        send_message(cfg, f"{header}\n{flat}"[:4000] if flat else header, target=target)
        return
    if not reply:
        send_message(cfg, tg.escape_html(header), plain=header, target=target)
        return

    chunks = tg.split_markdown(reply, CHUNK_CHARS)
    cap = max(1, int(cfg.get("max_messages", 8)))
    trimmed = max(0, len(chunks) - cap)
    chunks = chunks[:cap]
    fold = str(cfg.get("fold", "auto")).lower()
    for i, chunk in enumerate(chunks):
        folded = fold != "never" and (len(chunk) > FOLD_OVER or "\n" in chunk.strip())
        body = tg.md_to_html(chunk, block_code=not folded)
        if folded:
            body = f"<blockquote expandable>{body}</blockquote>"
        if trimmed and i == len(chunks) - 1:
            body += f"\n<i>(+{trimmed} more part{'s' if trimmed > 1 else ''} trimmed)</i>"
        head = tg.escape_html(header) if i == 0 else f"⋯ {i + 1}/{len(chunks)}"
        plain = f"{header}\n{chunk}" if i == 0 else chunk
        send_message(cfg, f"{head}\n{body}", plain=plain, target=target)


def ensure_bot(cfg: dict) -> None:
    """Keep the reply poller alive. The manifest's [[startup]] hook starts it with herdr;
    this is the safety net for a poller that died or a plugin enabled mid-session."""
    if cfg.get("replies", True) and tg.chat_id(cfg):
        tg.spawn_bot()


def swap_last_status(pane_id: str, status: str) -> str:
    """Record this pane's status and return the previous one ("" if unseen)."""
    seen = tg.read_state("last_status.json", {})
    prev = seen.get(pane_id, "")
    if len(seen) > 500:
        seen = {}
    seen[pane_id] = status
    tg.write_state("last_status.json", seen)
    return prev


def recently_sent(key: str, quiet_seconds: int) -> bool:
    """Debounce: skip if the same pane+status fired within quiet_seconds."""
    if quiet_seconds <= 0:
        return False
    now = time.time()
    seen = tg.read_state("last_sent.json", {})
    last = seen.get(key, 0)
    seen = {k: v for k, v in seen.items() if now - v < 3600}
    seen[key] = now
    tg.write_state("last_sent.json", seen)
    return now - last < quiet_seconds


def main() -> None:
    cfg = tg.load_config()

    if "--test" in sys.argv:
        if cfg is None:
            print(f"config.toml not found in {tg.CONFIG_DIR}", file=sys.stderr)
            sys.exit(1)
        send_message(cfg, "tg.notify test message from herdr 🔔")
        ensure_bot(cfg)
        return

    raw = os.environ.get("HERDR_PLUGIN_EVENT_JSON", "")
    if not raw:
        tg.log("no HERDR_PLUGIN_EVENT_JSON; exiting")
        return
    try:
        event = json.loads(raw)
    except json.JSONDecodeError as e:
        tg.log(f"bad event json: {e}")
        return
    # payload shape: {"event": "pane_agent_status_changed", "data": {...fields...}}
    if isinstance(event.get("data"), dict):
        event = event["data"]
    if os.environ.get("TG_NOTIFY_DEBUG") or "agent_status" not in event:
        tg.log(f"raw event: {raw[:500]}")

    status = event.get("agent_status", "unknown")
    pane_id = event.get("pane_id", "?")
    agent = event.get("display_agent") or event.get("agent") or "agent"
    title = event.get("title") or ""
    tg.log(f"event: pane={pane_id} agent={agent} status={status}")

    if cfg is None:
        return
    ensure_bot(cfg)
    # herdr only reports "done" for unfocused panes (attention semantics); a
    # focused pane goes working->idle directly. Treat that as done too.
    prev = swap_last_status(pane_id, status)
    if status == "idle" and prev == "working":
        status = "done"
        tg.log(f"working->idle on {pane_id}: treating as done (focused completion)")
    if status not in cfg.get("statuses", ["done"]):
        return
    agents = cfg.get("agents", [])
    if agents and (event.get("agent") or "") not in agents:
        return
    if recently_sent(f"{pane_id}:{status}", int(cfg.get("quiet_seconds", 5))):
        tg.log(f"debounced pane={pane_id} status={status}")
        return

    info = agent_info(pane_id)
    cwd = info.get("cwd") or info.get("foreground_cwd") or ""
    folder = tg.short_home(cwd)
    title = info.get("terminal_title_stripped") or title
    reply = tidy(reply_snippet(event.get("agent") or "", info.get("agent_session") or {}, cwd))

    # one line, so a phone's notification preview spends its few lines on the reply
    # rather than on metadata. ✅/❓ already says done/blocked; the word is redundant.
    emoji = tg.STATUS_EMOJI.get(status, "")
    head = " · ".join(p for p in (f"{emoji} {agent}".strip(), folder or pane_id,
                                  title[:int(cfg.get("title_chars", 48))]) if p)
    send_notification(cfg, head, reply,
                      {"pane": pane_id, "agent": agent, "folder": folder})


if __name__ == "__main__":
    main()
