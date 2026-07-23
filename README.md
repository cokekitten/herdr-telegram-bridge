# herdr-tg-notify

A [Herdr](https://herdr.dev) plugin that pushes a Telegram message when an AI agent finishes (`done`) or needs your input (`blocked`).

```
✅ claude done · ~/dev/myproject
📝 Fix the flaky integration test
💬 All three tests pass now. The race was in the fixture teardown…
```

Each notification includes the agent name, working directory, the pane title, and a snippet of the agent's final reply.

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

Agents not listed still get notifications — just without the 💬 line.

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

Options (see `config.example.toml`): `statuses` to notify on, per-`agents` filter, `quiet_seconds` debounce, `snippet_chars`, optional `proxy` / `api_base`.

Test it:

```sh
cd "$(herdr plugin config-dir tg.notify)/.." && herdr plugin action invoke tg.notify.test
```

or run the "Send Telegram test message" action from Herdr's plugin action menu.

## Notes

- Herdr only reports `done` for unfocused panes (attention semantics); a focused pane goes `working → idle` directly. The plugin tracks per-pane state and treats `working → idle` as a completion too, so you get notified either way.
- Requires Python 3.11+ (`tomllib`) and `curl`.
- Logs: `herdr plugin logs --plugin tg.notify`, plus `notify.log` in the plugin state dir.

## License

MIT
