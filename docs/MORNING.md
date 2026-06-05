# Morning startup — 3 steps

How to bring the dashboard back up the morning after `dash-down`.

---

## Quick version (after you `source ~/.zprofile` once)

```bash
dash-status     # 200 = up, anything else = down
dash-up         # start the server (leave terminal open)
# In Safari: http://127.0.0.1:8765/
```

To take it down again at night:

```bash
dash-down       # kills server + any cloudflared tunnel
```

---

## Long version (no aliases)

### 1. Open Terminal, check if the server is already up

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8765/healthz
```

- Prints **`200`** → server is already running. Skip to step 3.
- Prints **`000`** or nothing → server is dead. Go to step 2.

### 2. Start the server (only if step 1 said dead)

```bash
cd ~/alpine-data
source ~/.zprofile
HOST=0.0.0.0 .venv/bin/python server.py
```

Leave that terminal open. You'll see `Dashboard live on http://0.0.0.0:8765/`
when it's ready.

### 3. Open the dashboard

Safari → **http://127.0.0.1:8765/**

---

## Optional: start the public tunnel (so 🔗 Share links work)

If you want share links to work from a phone over cellular, **open a second
terminal** and run:

```bash
cloudflared tunnel --url http://localhost:8765
# or with the alias:
dash-tunnel
```

After ~5 sec it prints a `https://random-words.trycloudflare.com` URL.
Leave that terminal open too. Update the **Public host** field in the 🔗 Share
modal with the new URL (it changes per session — see `docs/SETUP.md §4` for
the named-tunnel recipe that pins a stable URL).

---

## Aliases reference (already in `~/.zprofile`)

| Command | What it does |
|---|---|
| `dash-status` | curl /healthz — prints 200 if up |
| `dash-up`     | `cd` to repo + `source ~/.zprofile` + start Flask server |
| `dash-down`   | kill server (port 8765) + kill cloudflared tunnel |
| `dash-tunnel` | start an ephemeral cloudflared quick-tunnel |

---

## Troubleshooting

**"Address already in use" when running `dash-up`**

The server is already running. Run `dash-status` to confirm — it'll print
`200`. Just open the URL in Safari, no need to start a new server.

**Safari shows "Failed to open page" or old data**

- Hard-reload: **⌥⌘R** (option-command-R), not plain ⌘R (which is Safari's
  Reader View)
- If that doesn't help, close the tab entirely and open a new one to
  `http://127.0.0.1:8765/`

**Macro chart shows "disabled — set FRED_API_KEY"**

`source ~/.zprofile` wasn't run before starting the server, so the env var
isn't in the server's environment. Kill (`dash-down`) and restart with
`dash-up` (which does the source for you).

**Share link recipient sees "expired"**

Either (a) the share token genuinely expired (default 3 days) or (b) you
revoked it. Mint a new one via the 🔗 Share button in the header.

**The tunnel URL stopped working**

The cloudflared terminal must stay open. If it `Ctrl+C`'d or your Mac
rebooted, the URL is dead. Run `dash-tunnel` to start a fresh one
(URL will change — update the Public host field in the Share modal).
