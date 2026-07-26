# herdr-telegram-bridge

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![herdr 0.7+](https://img.shields.io/badge/herdr-0.7%2B-8a2be2)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776ab)
![platforms: linux • macOS](https://img.shields.io/badge/platforms-linux%20%E2%80%A2%20macOS-informational)

**Walk away from your agents and answer them from your phone.** When a [herdr](https://herdr.dev) agent finishes or gets stuck, you get a Telegram message with what it actually said — rendered, not truncated. Reply to that message and your text goes straight into that agent's prompt. That is the whole interaction: **reply to the notification you got.**

```
✅ claude · sunrise · Fix the flaky integration test
   All three tests pass now. The race was in the fixture
   teardown — the DB handle outlived the transaction…

                        also add a regression test for it  ↰
➡️ sent to claude · ~/dev/sunrise

   … 40 seconds later …

✅ claude · sunrise · Fix the flaky integration test
   Added `test_teardown_ordering`. Full suite green: 41 passed.
```

No relay server, no tunnel, no port to open, no companion app. The plugin makes **outbound** long-poll requests to Telegram and nothing else — which is exactly why it works from a laptop on hotel wifi.

## Why you'd want it

- **Reply, don't command.** Every notification is addressed to the agent it came from, so replying to it routes back to that pane. Ten agents running, ten conversations — Telegram's own reply threading keeps them straight. Commands exist, but you rarely need one.
- **The whole answer, readable.** The agent's final reply is rendered into Telegram's markup — headings, bullets, links, syntax-labelled code, monospaced tables. Long answers fold into an expandable quote so a chatty agent can't flood the chat, and anything past Telegram's 4096-character ceiling is split across messages that all remain replyable.
- **Send it a screenshot.** Photos and files land on disk and the agent is handed the path — point it at a stack trace, a design mock, or a log without getting to a keyboard.
- **Nothing to run.** No relay, no tunnel, no daemon you have to remember to start. The poller is launched by herdr itself and self-heals if it dies.
- **It only listens to you.** Only the chat id you configure is accepted; everything else is logged and dropped. `allowed_user_ids` narrows it further to specific accounts.

## What it does

**Notifications** — one line of context (status, agent, directory, pane title) and then the agent's final reply. The header is deliberately short: a phone's notification preview gets only a few lines and they should go to the reply. herdr reports `done` only for unfocused panes, so the plugin also tracks `working → idle` per pane and treats that as a completion — you get told either way. `statuses`, an `agents` allowlist and a `quiet_seconds` debounce control what is worth interrupting you for.

**Replies** — your text is submitted with `herdr agent prompt`; the agent starts working, finishes, and the next notification arrives on its own. A message that isn't a reply goes to the most recently active agent, so a back-and-forth doesn't need long-pressing every time.

**Commands** — published with `setMyCommands`, so they show up in Telegram's menu button:

| | |
|---|---|
| `/agents` | every agent as a button, grouped under its working directory, most recently active group first — with status, agent and pane title, because agents often share a directory and the title is what tells them apart |
| `/read [lines]` | that agent's recent terminal output |
| `/esc` | send Escape to interrupt it |
| `/help` | how replying works |

**Attachments** — photos, documents, voice notes and video are saved to `<state dir>/inbox/`, and the agent is prompted with your caption plus the absolute path. Sender-supplied filenames are flattened to a plain basename, and the inbox is swept of anything older than `inbox_days` (7) every six hours.

**Reply text** is read from each agent CLI's own session storage:

| Agent | Source |
|---|---|
| claude (Claude Code) | `~/.claude/projects/*/<session>.jsonl` |
| codex | `~/.codex/sessions/…/rollout-*.jsonl` |
| pi | session path reported by herdr |
| opencode | `~/.local/share/opencode/opencode.db` |
| grok | `~/.grok/sessions/<cwd>/…/chat_history.jsonl`, session pinned via the pane's process |
| kimi (Kimi Code) | `~/.kimi-code/sessions/…/wire.jsonl` |
| hermes | `~/.hermes/state.db` |

Anything else still notifies and still takes replies — just without the transcript excerpt.

Not every CLI sets a useful terminal title: pi shows `π - <user>`, hermes shows `π - <dir>`, codex shows the directory, a fresh Claude Code shows its own name. When herdr's title says nothing, the session's opening prompt is used instead — skipping the synthetic context (`AGENTS.md` dumps, `<user_info>` blocks) that agents front-load as user messages. A pane that has not been asked anything yet legitimately has no title either way.

## Install

```sh
herdr plugin install cokekitten/herdr-telegram-bridge
```

Plugins hot-load, so no herdr restart is needed. Requires Python 3.11+ (`tomllib`) and `curl`.

## Configure

Create a bot with [@BotFather](https://t.me/BotFather), send it any message, then read your chat id from `https://api.telegram.org/bot<TOKEN>/getUpdates`.

```sh
cp config.example.toml "$(herdr plugin config-dir telegram.bridge)/config.toml"
$EDITOR "$(herdr plugin config-dir telegram.bridge)/config.toml"    # bot_token, chat_id
chmod 600 "$(herdr plugin config-dir telegram.bridge)/config.toml"  # it holds your token
```

Then send a test message — either the **Send Telegram test message** action in herdr's plugin menu, or:

```sh
herdr plugin action invoke telegram.bridge.test
```

Everything else has a working default. [`config.example.toml`](config.example.toml) documents the full set: which `statuses` notify, an `agents` filter, `settle_ms`, `quiet_seconds`, `snippet_chars`, `markdown` / `fold` / `max_messages`, `replies` / `reply_to_latest` / `allowed_user_ids` / `read_lines`, `accept_files` / `inbox_days` / `max_file_mb`, and `proxy` / `api_base` if you need to reach Telegram through something else.

To use the bot in a group rather than a private chat, disable its privacy mode in @BotFather (`/setprivacy`) — and note that everyone in that group can then drive your agents unless you set `allowed_user_ids`.

## How it works

Telegram cannot push to a laptop, so `bot.py` long-polls `getUpdates` as a detached singleton. The manifest's `[[startup]]` hook launches it with herdr, the status-change hook re-checks it on every agent transition so a dead poller comes back by itself, and a `flock` keeps exactly one alive. Two actions in herdr's plugin menu — **poller status** and **restart poller** — cover the rest (`python3 bot.py --status|--restart|--stop`).

Routing is a `message id → pane` map recorded as each notification is sent, capped at `msg_map_limit` entries. Replying to something older than the cap reports an error rather than quietly retargeting whichever agent happens to be current — as does replying to an agent whose pane has since been closed. Nothing is ever redirected on your behalf.

A few behaviours of the surrounding systems are worth writing down, because each one caused a bug:

- **`herdr agent prompt` writes the text and the Enter in one burst.** A busy agent queues that fine, but an *idle* Claude Code folds the trailing Enter into its paste-detection window and strands the text in the input box. So when the agent wasn't already working, the poller waits `submit_wait_ms` for it to start and presses Enter once more if it never does.
- **Telegram rejects messages over 4096 characters** rather than splitting them the way the client does when you paste a wall of text. Splitting therefore happens here, and every part maps back to the same agent.
- **Telegram won't nest a code block inside a blockquote** — the client ends the quote, emits the block, then opens a fresh one, so one reply arrives as several separately-collapsing pieces. Inside a fold, code and tables use inline monospace instead; `fold = "never"` trades folding away for syntax highlighting and copy buttons.
- **An untagged `<pre>` gets its language guessed** by the client and highlighted accordingly, which turns a plain table into confetti. Every block therefore carries an explicit tag, `plaintext` included.
- **herdr infers agent state by reading the terminal, and that flaps.** grok flashes `blocked` for about a second as a conversation starts, and a pane can blink through `idle` mid-turn. Every notification therefore waits `settle_ms` and re-reads the pane before sending, dropping the transition if it did not hold.
- **grok files sessions under a url-encoded cwd, not a session id**, and herdr reports no session reference for it — so several conversations share a directory, and a brand new one has no transcript until its first reply lands. Picking the newest by mtime hands back the *previous* conversation, so the session is instead pinned by asking the pane's grok process which `events.jsonl` it holds open.
- **`getUpdates` serves ~24h of backlog on first run.** That is skipped, so the messages you sent the bot while setting up `chat_id` never arrive as prompts.

Logs are in `bridge.log` in the plugin state dir, plus `herdr plugin logs --plugin telegram.bridge`.

## Limits

- Telegram bots cannot download files larger than **20 MB**; `max_file_mb` only lowers that further.
- The injection channel is text, so an attachment reaches the agent as a *path*, not as pixels. Agents that open files themselves (Claude Code among them) handle that transparently; others just see a path.
- Interrupting is a plain Escape keypress. If an agent is sitting on a permission dialog rather than a prompt, `/esc` and the Enter fallback act on that dialog — set `submit_fallback = false` if you would rather they never did.

## License

MIT — see [LICENSE](LICENSE).
