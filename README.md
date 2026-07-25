# herdr-tg-notify

A [Herdr](https://herdr.dev) plugin that pushes a Telegram message when an AI agent finishes (`done`) or needs your input (`blocked`) — and lets you reply to that message to send text straight back into the agent.

```
✅ claude · ~/dev/myproject · Fix the flaky integration test
All three tests pass now. The race was in the fixture teardown…

   ↳ you: also add a regression test for it
➡️ sent to claude · ~/dev/myproject
```

One header line — status, agent, working directory, pane title — then the agent's final reply. The header is deliberately short: a phone's notification preview only gets a few lines, and they should go to the reply.

The reply is rendered as markdown into the small tag set Telegram accepts — headings become bold, lists become bullets, code keeps its fences and syntax label, tables become monospaced blocks. Anything longer than a line or two goes in an expandable quote, so a chatty agent doesn't flood the chat. Telegram rejects messages over 4096 characters rather than splitting them, so the plugin splits them itself and maps every part back to the same agent — replying to any part reaches it. `snippet_chars` caps the reply if you'd rather keep notifications short; `markdown = false` sends flat text.

## Replying

Reply to any notification and its text is submitted to that agent (`herdr agent prompt`). The agent starts working, finishes, and you get the next notification — the loop closes by itself.

A plain message (not a reply) goes to the most recently active agent, so a back-and-forth needs no long-pressing. Set `reply_to_latest = false` to require an explicit reply instead.

| Command | |
|---|---|
| `/agents` | list agents with a button each; tap one to aim plain messages at it |
| `/read [lines]` | that agent's recent terminal output (default 40 lines) |
| `/esc` | send Escape to interrupt the agent |
| `/help` | usage |

`/read` and `/esc` act on the agent you replied to, or on the current one if you didn't reply. The commands are published with `setMyCommands`, so Telegram shows them in the menu button next to the input box.

Photos and files work too: send one (a screenshot of a stack trace, say) and it lands in `<state dir>/inbox/`, with the agent handed its absolute path and your caption. herdr can only inject text, so the agent opens the file itself rather than receiving the bytes. The inbox is swept of anything older than `inbox_days` (7) every six hours, and sender-supplied filenames are flattened to a plain basename so nothing can be written outside it. Telegram bots cannot download files over 20 MB at all.

Only the configured `chat_id` is accepted; `allowed_user_ids` narrows it further to specific Telegram accounts. Everything else is logged and dropped.

## Reply snippets

The final-reply snippet is extracted from each agent CLI's local session storage. Currently supported:

| Agent | Source |
|---|---|
| claude (Claude Code) | `~/.claude/projects/*/<session>.jsonl` |
| codex | `~/.codex/sessions/…/rollout-*.jsonl` |
| pi | session path reported by Herdr |
| opencode | `~/.local/share/opencode/opencode.db` |
| grok | `~/.grok/sessions/<cwd>/…/chat_history.jsonl` |
| kimi (Kimi Code) | `~/.kimi-code/sessions/…/wire.jsonl` |
| hermes | `~/.hermes/state.db` |

Agents not listed still get notifications — just without the reply body. Replying, and sending files, works for every agent regardless.

## Install

```sh
herdr plugin install cokekitten/herdr-tg-notify
```

Or link a local checkout while developing:

```sh
herdr plugin link /path/to/herdr-tg-notify
```

No Herdr restart needed — plugins hot-load.

## Configure

Create a bot with [@BotFather](https://t.me/BotFather), send your bot any message, then get your chat id from `https://api.telegram.org/bot<TOKEN>/getUpdates`.

```sh
cp config.example.toml "$(herdr plugin config-dir tg.notify)/config.toml"
# then edit bot_token / chat_id
```

Options (see `config.example.toml`): `statuses` to notify on, per-`agents` filter, `quiet_seconds` debounce, `snippet_chars`, `markdown` / `fold` / `max_messages`, `replies` / `reply_to_latest` / `allowed_user_ids` / `read_lines`, `accept_files` / `inbox_days` / `max_file_mb`, optional `proxy` / `api_base`.

Turn off privacy mode for your bot in @BotFather (`/setprivacy` → Disable) if you want to use it in a group rather than a private chat.

Test it:

```sh
cd "$(herdr plugin config-dir tg.notify)/.." && herdr plugin action invoke tg.notify.test
```

or run the "Send Telegram test message" action from Herdr's plugin action menu.

## How replies get in

Telegram has no way to push to a laptop, so a small poller (`bot.py`) long-polls `getUpdates`. It is started by the manifest's `[[startup]]` hook when Herdr starts, and the status-change hook re-checks it on every event, so a poller that died comes back on the next agent transition. A `flock` on the plugin state dir keeps exactly one running.

Two actions in Herdr's plugin menu manage it: **Telegram replies: poller status** and **Telegram replies: restart poller** (also `python3 bot.py --status|--restart|--stop` from the plugin dir).

Sent notifications are recorded in `msg_targets.json` as message id → pane, which is what makes a reply find its agent. Because the file is rewritten whole on every send it is capped at `msg_map_limit` (1000) entries; replying to a notification older than that reports an error rather than quietly rerouting your message to whichever agent happens to be current.

Telegram will not nest a code block inside a blockquote — the client ends the quote, emits the block, and opens a fresh quote, so one reply arrives as several separately-collapsing pieces. Inside a fold, code and tables therefore use inline monospace instead (`fold = "never"` gives up folding to get syntax highlighting and copy buttons back). Code blocks always carry an explicit language tag, including `plaintext` for tables, because an untagged block gets a language auto-guessed by the client and syntax-highlighted into confetti.

`herdr agent prompt` writes the text and the Enter in one burst. A busy agent queues that fine, but an *idle* Claude Code folds the trailing Enter into its paste-detection window and leaves the text staged in the input box. So when the agent wasn't already working, the poller waits `submit_wait_ms` for it to start and presses Enter once more if it never does — `submit_fallback = false` turns that off.

On its very first run the poller skips whatever Telegram already has queued (the API holds updates for ~24h), so the messages you sent the bot while setting up `chat_id` don't arrive as prompts.

## Notes

- Herdr only reports `done` for unfocused panes (attention semantics); a focused pane goes `working → idle` directly. The plugin tracks per-pane state and treats `working → idle` as a completion too, so you get notified either way.
- Requires Python 3.11+ (`tomllib`) and `curl`.
- Logs: `herdr plugin logs --plugin tg.notify`, plus `notify.log` in the plugin state dir (which also holds the poller's log).

## License

MIT
