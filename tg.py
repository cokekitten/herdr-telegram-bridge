#!/usr/bin/env python3
"""Shared plumbing for the tg.notify plugin.

Imported by notify.py (the status-change hook) and bot.py (the reply poller):
plugin paths, logging, config, the Telegram API call, the herdr CLI call, and the
message -> agent map that lets a Telegram reply find its way back to a pane.
"""

import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
import urllib.parse
import urllib.request

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.environ.get("HERDR_PLUGIN_STATE_DIR") or PLUGIN_DIR
CONFIG_DIR = os.environ.get("HERDR_PLUGIN_CONFIG_DIR") or PLUGIN_DIR
LOG_PATH = os.path.join(STATE_DIR, "notify.log")
LOG_MAX_BYTES = 2_000_000

STATUS_EMOJI = {"done": "✅", "blocked": "❓", "idle": "💤", "working": "⏳", "unknown": "❔"}

MSG_MAP = "msg_targets.json"
CURRENT_TARGET = "current_target.json"
MSG_MAP_LIMIT = 1000  # override per-send with the msg_map_limit config key
LOCK_NAME = "bot.lock"


def log(msg: str) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    try:
        if os.path.getsize(LOG_PATH) > LOG_MAX_BYTES:
            os.replace(LOG_PATH, LOG_PATH + ".1")
    except OSError:
        pass
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


def chat_id(cfg: dict) -> str:
    value = str(cfg.get("chat_id", ""))
    return "" if "REPLACE" in value else value


def short_home(path: str) -> str:
    return path.replace(os.path.expanduser("~"), "~") if path else ""


# --- herdr CLI ---------------------------------------------------------------


def herdr_bin() -> str:
    return os.environ.get("HERDR_BIN_PATH") or "herdr"


def herdr_run(*args: str, timeout: int = 30) -> tuple[str, str]:
    """Run a herdr subcommand -> (stdout, error message).

    herdr reports failures as {"error": {...}} on stdout *with exit code 0*, so the
    error string — not the exit code — is what callers have to check.
    """
    try:
        out = subprocess.run([herdr_bin(), *args], capture_output=True, timeout=timeout, check=False)
    except Exception as e:
        return "", repr(e)
    text = out.stdout.decode(errors="replace").strip()
    try:
        body = json.loads(text)
    except ValueError:
        body = None
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        return "", body["error"].get("message") or body["error"].get("code") or "herdr error"
    if out.returncode != 0:
        return "", (out.stderr.decode(errors="replace").strip() or f"exit {out.returncode}")[:300]
    return text, ""


def herdr_json(*args: str, timeout: int = 30) -> tuple[dict, str]:
    """herdr_run + unwrap the {"result": {...}} envelope."""
    text, err = herdr_run(*args, timeout=timeout)
    if err:
        return {}, err
    try:
        return json.loads(text).get("result", {}), ""
    except ValueError as e:
        return {}, repr(e)


# --- Telegram API ------------------------------------------------------------


def api(cfg: dict, method: str, params: dict, timeout: int = 20, retries: int = 3):
    """Call a Telegram Bot API method -> (result, error message).

    curl first: empirically the only reliably working TLS path on this box
    (python/OpenSSL handshakes to api.telegram.org get reset intermittently).
    urllib with the system proxy is the fallback.
    """
    token = str(cfg.get("bot_token", ""))
    if not token or "REPLACE" in token:
        return None, "bot_token not set"
    base = cfg.get("api_base", "https://api.telegram.org").rstrip("/")
    url = f"{base}/bot{token}/{method}"

    cmd = ["curl", "-sm", str(timeout), url]
    for k, v in params.items():
        cmd += ["--data-urlencode", f"{k}={v}"]
    proxy = cfg.get("proxy", "")
    if proxy:
        cmd[1:1] = ["-x", proxy]

    err = ""
    for attempt in range(max(1, retries)):
        try:
            out = subprocess.run(cmd, capture_output=True, timeout=timeout + 10, check=False)
            body = json.loads(out.stdout.decode() or "{}")
            if body.get("ok"):
                return body.get("result"), ""
            err = f"curl exit={out.returncode} body={out.stdout[:200]!r}"
        except Exception as e:
            err = repr(e)
        if attempt + 1 < max(1, retries):
            time.sleep(1 + attempt)

    try:
        data = urllib.parse.urlencode({k: str(v) for k, v in params.items()}).encode()
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=timeout + 5) as resp:
            body = json.loads(resp.read().decode())
        if body.get("ok"):
            return body.get("result"), ""
        return None, f"{err}; api: {body.get('description')}"
    except Exception as e:
        return None, f"{err}; urllib: {e!r}"


def redact(text: str, cfg: dict) -> str:
    """Blank the bot token out of a diagnostic string — it rides in the file URL path."""
    token = str(cfg.get("bot_token", ""))
    return text.replace(token, "<token>") if token else text


def inbox_dir() -> str:
    path = os.path.join(STATE_DIR, "inbox")
    os.makedirs(path, exist_ok=True)
    return path


def safe_name(name: str, limit: int = 80) -> str:
    """A sender-supplied filename is untrusted: keep a flat, boring basename so nothing
    can climb out of the inbox. Truncation keeps the suffix — an agent handed a path
    still needs to know it is looking at a .png."""
    name = re.sub(r"[^\w.\-]+", "_", os.path.basename(name or "")).strip("._")
    if not name:
        return "file"
    if len(name) > limit:
        stem, dot, ext = name.rpartition(".")
        ext = f".{ext}" if dot and 0 < len(ext) <= 10 else ""
        name = (stem or name)[:limit - len(ext)] + ext
    return name


def download(cfg: dict, file_path: str, dest: str) -> str:
    """Fetch a Telegram file to dest. Returns an error message, or ""."""
    token = str(cfg.get("bot_token", ""))
    base = cfg.get("api_base", "https://api.telegram.org").rstrip("/")
    cmd = ["curl", "-sfL", "-m", "120", "-o", dest, f"{base}/file/bot{token}/{file_path}"]
    proxy = cfg.get("proxy", "")
    if proxy:
        cmd[1:1] = ["-x", proxy]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=180, check=False)
    except Exception as e:
        return redact(repr(e), cfg)
    if out.returncode != 0:
        detail = out.stderr.decode(errors="replace").strip()[:160]
        return redact(f"curl exit={out.returncode} {detail}", cfg)
    try:
        return "" if os.path.getsize(dest) else "downloaded 0 bytes"
    except OSError as e:
        return repr(e)


def sweep_inbox(days: float) -> int:
    """Delete inbox files older than `days`. Returns how many went."""
    if days <= 0:
        return 0
    cutoff = time.time() - days * 86400
    gone = 0
    for name in os.listdir(inbox_dir()):
        path = os.path.join(inbox_dir(), name)
        try:
            if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                os.remove(path)
                gone += 1
        except OSError:
            pass
    return gone


# --- markdown -> Telegram HTML -----------------------------------------------
#
# Telegram accepts a fixed, tiny set of tags — b/i/u/s/code/pre/a/blockquote — and
# nothing else, so agent replies (GitHub-flavoured markdown) get rendered down to it:
# headings become bold, lists become bullets, tables become monospaced blocks.

TABLE_RULE = re.compile(r"^\s*\|?[\s:|-]*-[\s:|-]*\|[\s:|-]*$")
FENCE = re.compile(r"^\s*(```|~~~)(.*)$")
HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
BULLET = re.compile(r"^(\s*)[-*+][ \t]+")
QUOTE = re.compile(r"^\s{0,3}(?:&gt;|>)[ \t]?")  # runs after escaping, so match both forms
RULE = re.compile(r"^\s{0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$")
INLINE_CODE = re.compile(r"`([^`\n]+)`")
LINK = re.compile(r"\[([^\]\n]*)\]\(([^)\s]+)\)")
BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)
BOLD_ALT = re.compile(r"(?<!\w)__(.+?)__(?!\w)", re.S)
ITALIC = re.compile(r"(?<![\w*])\*([^*\n]+?)\*(?![\w*])")
ITALIC_ALT = re.compile(r"(?<![\w_])_([^_\n]+?)_(?![\w_])")
STRIKE = re.compile(r"~~(.+?)~~", re.S)
STASHED = re.compile(r"\x00(\d+)\x00")


def escape_html(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _blocks(text: str):
    """Split markdown into ("code"|"table"|"text", body, info) runs."""
    lines = text.split("\n")
    i, plain = 0, []
    while i < len(lines):
        fence = FENCE.match(lines[i])
        table = (
            "|" in lines[i]
            and i + 1 < len(lines)
            and TABLE_RULE.match(lines[i + 1])
            and "|" in lines[i + 1]
        )
        if fence:
            if plain:
                yield "text", "\n".join(plain), ""
                plain = []
            marker, info, i = fence.group(1), fence.group(2).strip(), i + 1
            body = []
            while i < len(lines) and not lines[i].strip().startswith(marker):
                body.append(lines[i])
                i += 1
            i += 1  # closing fence (or end of input)
            yield "code", "\n".join(body), info
        elif table:
            if plain:
                yield "text", "\n".join(plain), ""
                plain = []
            body = []
            while i < len(lines) and "|" in lines[i]:
                body.append(lines[i].strip())
                i += 1
            yield "table", "\n".join(body), ""
        else:
            plain.append(lines[i])
            i += 1
    if plain:
        yield "text", "\n".join(plain), ""


def _render_text(text: str) -> str:
    stash: list[str] = []

    def keep(m):
        stash.append(m.group(1))
        return f"\x00{len(stash) - 1}\x00"

    text = INLINE_CODE.sub(keep, text)
    text = escape_html(text)

    out = []
    for ln in text.split("\n"):
        if RULE.match(ln):
            out.append("─" * 24)
            continue
        head = HEADING.match(ln)
        if head:
            # strip inner ** so the later bold pass can't nest <b> inside <b>
            out.append(f"<b>{head.group(1).replace('**', '')}</b>")
            continue
        # a real <blockquote> would nest inside the one wrapping the whole reply
        ln = QUOTE.sub("│ ", ln)
        out.append(BULLET.sub(lambda m: m.group(1) + "• ", ln))
    text = "\n".join(out)

    text = LINK.sub(lambda m: f'<a href="{m.group(2)}">{m.group(1) or m.group(2)}</a>', text)
    text = BOLD.sub(r"<b>\1</b>", text)
    text = BOLD_ALT.sub(r"<b>\1</b>", text)
    text = ITALIC.sub(r"<i>\1</i>", text)
    text = ITALIC_ALT.sub(r"<i>\1</i>", text)
    text = STRIKE.sub(r"<s>\1</s>", text)
    return STASHED.sub(lambda m: f"<code>{escape_html(stash[int(m.group(1))])}</code>", text)


def md_to_html(text: str, block_code: bool = True) -> str:
    """Render markdown into the HTML subset Telegram's parse_mode=HTML accepts.

    block_code=False swaps <pre> for inline <code>. <pre> and <blockquote> are both
    block entities and Telegram forbids nesting them: a <pre> inside a quote makes the
    client cut the quote short, emit the block, then start a fresh quote — one reply
    turns into several separately-collapsing pieces. Inline <code> nests fine, so it is
    what keeps a folded reply in one piece (at the cost of syntax highlighting).
    """
    out = []
    for kind, body, info in _blocks(text):
        if kind in ("code", "table"):
            if not block_code:
                out.append(f"<code>{escape_html(body)}</code>")
                continue
            # tag tables too: an untagged <pre> gets a language auto-guessed by the
            # client, which then syntax-highlights a plain table into confetti
            lang = info.split()[0] if (kind == "code" and info.split()) else "plaintext"
            out.append(f'<pre><code class="language-{escape_html(lang)}">'
                       f"{escape_html(body)}</code></pre>")
        elif body.strip():
            out.append(_render_text(body))
    return "\n".join(out)


def split_markdown(text: str, limit: int) -> list[str]:
    """Chunk markdown so each piece fits one Telegram message, never cutting a fenced
    code block in half — an open fence is closed and reopened across the seam."""
    chunks: list[str] = []
    cur: list[str] = []
    size = 0
    fence = info = ""

    def flush(reopen: bool) -> None:
        nonlocal cur, size
        if fence:
            cur.append(fence)
        chunks.append("\n".join(cur))
        cur = [f"{fence}{info}"] if (reopen and fence) else []
        size = sum(len(x) + 1 for x in cur)

    for line in text.split("\n"):
        while len(line) > limit:  # a single monstrous line
            if cur:
                flush(True)
            cut = limit - size
            cur.append(line[:cut])
            size += cut + 1
            flush(True)
            line = line[cut:]
        if cur and size + len(line) + 1 > limit:
            flush(True)
        m = FENCE.match(line)
        if m:
            if fence and line.strip().startswith(fence):
                fence = info = ""
            elif not fence:
                fence, info = m.group(1), m.group(2).strip()
        cur.append(line)
        size += len(line) + 1
    if cur:
        flush(False)
    return [c for c in chunks if c.strip()]


# --- state files -------------------------------------------------------------


def state_path(name: str) -> str:
    return os.path.join(STATE_DIR, name)


def read_state(name: str, default):
    try:
        with open(state_path(name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def write_state(name: str, obj) -> None:
    """Atomic write: the hook and the poller are separate processes sharing these files."""
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = state_path(f"{name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    os.replace(tmp, state_path(name))


# --- message -> agent routing ------------------------------------------------
#
# A target is {"pane": <pane id>, "agent": <display name>, "folder": <~-relative cwd>}.


def label(target: dict) -> str:
    name = target.get("agent") or "agent"
    where = target.get("folder") or target.get("pane") or ""
    return f"{name} · {where}" if where else name


def remember_target(message_id, target: dict, limit: int = MSG_MAP_LIMIT) -> None:
    """Record which agent a bot message is about, so a reply to it routes back there.

    The map is rewritten whole on every send, so it is capped rather than unbounded;
    past the cap the oldest entries drop and replying to that far back reports an
    error instead of guessing. Raise msg_map_limit if you reply to ancient messages.
    """
    if not message_id or not target.get("pane"):
        return
    entry = {**target, "ts": int(time.time())}
    seen = read_state(MSG_MAP, {})
    seen[str(message_id)] = entry
    if len(seen) > limit:
        seen = dict(sorted(seen.items(), key=lambda kv: kv[1].get("ts", 0))[-limit:])
    write_state(MSG_MAP, seen)
    write_state(CURRENT_TARGET, entry)


def target_for_reply(message_id) -> dict:
    return read_state(MSG_MAP, {}).get(str(message_id), {}) if message_id else {}


def current_target() -> dict:
    return read_state(CURRENT_TARGET, {})


def set_current_target(target: dict) -> None:
    if target.get("pane"):
        write_state(CURRENT_TARGET, {**target, "ts": int(time.time())})


# --- poller lifecycle --------------------------------------------------------


def bot_lock():
    """Take the poller singleton lock. Returns the held file (keep the reference alive!)
    or None when another process holds it."""
    os.makedirs(STATE_DIR, exist_ok=True)
    f = open(state_path(LOCK_NAME), "a+", encoding="utf-8")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def bot_running() -> bool:
    f = bot_lock()
    if f is None:
        return True
    f.close()  # closing drops the lock we just took
    return False


def bot_pid() -> int:
    try:
        with open(state_path(LOCK_NAME), encoding="utf-8") as f:
            return int(f.read().strip() or 0)
    except (OSError, ValueError):
        return 0


def spawn_bot() -> bool:
    """Start the reply poller detached, so it outlives the herdr hook that spawned it.

    The resolved plugin dirs are passed down explicitly: a poller started from the
    manifest's [[startup]] hook must agree with the event hook on where state lives.
    """
    if bot_running():
        return False
    env = {
        **os.environ,
        "HERDR_PLUGIN_STATE_DIR": STATE_DIR,
        "HERDR_PLUGIN_CONFIG_DIR": CONFIG_DIR,
        "HERDR_BIN_PATH": shutil.which(herdr_bin()) or herdr_bin(),
    }
    devnull = os.open(os.devnull, os.O_RDWR)
    try:
        subprocess.Popen(
            [sys.executable, os.path.join(PLUGIN_DIR, "bot.py"), "--daemon"],
            cwd=PLUGIN_DIR, env=env, stdin=devnull, stdout=devnull, stderr=devnull,
            start_new_session=True,
        )
    except Exception as e:
        os.close(devnull)
        log(f"bot: spawn failed: {e!r}")
        return False
    os.close(devnull)
    log("bot: spawned reply poller")
    return True
