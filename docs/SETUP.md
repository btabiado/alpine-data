# Setup guides

User-action items that can't be installed for you, with copy-paste-ready steps.

---

## 0. Add a password to the dashboard (HTTP Basic Auth)

The local Flask server has zero auth by default — anyone reachable over
your network (Wi-Fi or Tailscale) can view your dashboard. To require a
login:

### Pick a strong password and set env vars
```bash
# Open ~/.zprofile in any editor and add:
export DASH_USER="btabiado"
export DASH_PASS="<a strong password — use 1Password, etc.>"

# Reload your shell env:
source ~/.zprofile

# Restart the server so it picks up the new env:
lsof -ti:8765 | xargs kill -9
cd ~/alpine-data
HOST=0.0.0.0 .venv/bin/python server.py
```

### Verify
Visit **http://127.0.0.1:8765/**. The browser pops a username/password
dialog. Enter `btabiado` + your password. After success, the browser
caches it for the session — no further prompts.

### What's protected vs not
- **Protected:** `/`, `/api/data`, `/api/refresh`, `/api/chat`,
  `/api/upload-csv`, `/api/seed-etf`, `/bookmarklet`
- **NOT protected:** `/healthz` (so uptime monitors / Tailscale probes
  work without creds)

### The bookmarklet still works
When auth is on, the `/bookmarklet` page embeds your credentials
directly into the generated bookmarklet (only visible to you after
you've logged in). The bookmark itself sends `Authorization: Basic ...`
on every cross-origin POST, so the Farside import workflow keeps working.

If you regenerate the bookmarklet (e.g. you change your password),
re-visit **/bookmarklet** and re-drag the orange button into your
bookmarks bar.

### Reverting to no-auth
Just unset the env vars (or comment them out in `~/.zprofile`) and
restart the server. With no `DASH_USER`/`DASH_PASS` set, auth is bypassed.

---

## 1. Enable real LLM chat (set `ANTHROPIC_API_KEY`)

The chat dock works out-of-the-box in **rule-based fallback** mode — it
pulls real numbers from the dashboard payload. To unlock full Claude
LLM responses (model: `claude-haiku-4-5` by default):

### Get a key
1. Sign in at https://console.anthropic.com
2. Left sidebar → **API Keys** → **Create key**
3. Name it something like `etf-dashboard-local`
4. Copy the `sk-ant-...` value (you can't see it again later)

### Activate it on your Mac
```bash
# Add to your shell startup so it persists across reboots:
echo 'export ANTHROPIC_API_KEY="sk-ant-..."' >> ~/.zprofile
source ~/.zprofile

# Verify it's set:
echo $ANTHROPIC_API_KEY | head -c 12   # should print sk-ant-...

# Restart the dashboard server so it picks up the env var:
lsof -ti:8765 | xargs kill -9
cd ~/alpine-data
HOST=0.0.0.0 .venv/bin/python server.py
```

### Verify it took
Open the chat dock (💬 bottom-right) and ask **"summarise the insights for me"**. If the response starts with **`(LLM offline — using rule-based fallback...)`** the key wasn't picked up. Otherwise you're on Claude.

### Optional: change the model
```bash
export CHAT_MODEL=claude-sonnet-4-5    # smarter, slower, costlier
export CHAT_MODEL=claude-haiku-4-5     # default — fast and cheap
```

### Cost
Haiku is roughly **$0.001 per question** at typical lengths. A heavy month
of chatting is still under $1. Anthropic's billing dashboard tracks it.

### Optional: live LunarCrush tool calls in chat (MCP)
If you've also set `LUNARCRUSH_API_KEY` (see §7), the chat dock attaches
LunarCrush's official MCP server to its Anthropic call. Claude can then
fetch fresh social sentiment / Galaxy Score / trending coins mid-chat
instead of relying on the cached snapshot. Full details in **§8**.

---

## 2. Tailscale: phone-from-anywhere access

Right now your phone can reach the dashboard at `http://192.168.12.114:8765/`
**only on the same Wi-Fi as your Mac**. Tailscale fixes that with a
zero-config encrypted tunnel — works on cellular, hotel Wi-Fi, anywhere.

### Mac side (5 min)
1. Download: https://tailscale.com/download/mac
2. Install, open it. It'll ask you to sign in — use Google/Apple/Microsoft
   account (free for personal use, up to 100 devices)
3. The menu-bar icon should show **green** and your machine appears as
   something like `bryantabiados-macbook-pro` with a `100.x.x.x` IP

### Phone side (2 min)
1. Install the **Tailscale** app from the App Store / Play Store
2. Sign in with the same account
3. Enable VPN when prompted

### Use it
- On your phone, open: `http://bryantabiados-macbook-pro:8765/`
  (or the `100.x.x.x` IP shown in the Tailscale menu on your Mac)
- Works from anywhere your phone has internet — coffee shop, plane Wi-Fi, etc.

### Security note
Tailscale is private to your account by default. Nothing is exposed to
the public internet. Don't share your auth tokens.

---

## 3. GitHub Pages: published static dashboard

A workflow at `.github/workflows/pages.yml` builds and deploys a static
snapshot of `dashboard.html` on every push to `main`. To enable it:

### One-time setup
1. Visit https://github.com/btabiado/alpine-data/settings/pages
2. Under **Build and deployment**, set **Source = GitHub Actions**
3. Save

### Push to deploy
The workflow runs on every `git push origin main`. After ~60 sec:
- Check https://github.com/btabiado/alpine-data/actions → "pages" workflow → green ✓
- Your dashboard is live at: **https://btabiado.github.io/alpine-data/**

### Important caveats
- The Pages version is a **static snapshot** — no live `/api/refresh`,
  no chat dock backend, no `/api/upload-csv`. Charts work, KPI cards work,
  signal scores reflect the moment of generation.
- To publish with **live market data**, run locally before pushing:
  ```bash
  cd ~/alpine-data
  HOST=0.0.0.0 .venv/bin/python app.py --fetch-market --no-open
  git add data/market.json data/whale.json
  git commit -m "Refresh data snapshot"
  git push
  ```
- The repo is currently **private**, which means Pages will require a
  GitHub Pro subscription to host. Either flip the repo to public on
  https://github.com/btabiado/alpine-data/settings (scroll to
  bottom → Change visibility), or upgrade to Pro.

### Disable
Don't want public Pages? Delete `.github/workflows/pages.yml` and the
workflow stops running.

---

## 4. Share links (read-only, time-bounded) over Cloudflare Tunnel

You can mint a public URL that anyone can open in a browser — no login —
that shows your live dashboard for a fixed window (default 3 days) and then
self-destructs. Perfect for sending via text.

Two pieces:
1. **The share-token system** — already built into the dashboard. Mint via
   the **🔗 Share** button in the top-right of the dashboard, or via the
   `share.py` CLI. Tokens are unguessable (24 random bytes ≈ 192 bits) and
   auto-expire.
2. **A public tunnel** — Cloudflare Tunnel exposes your local server to a
   public URL. Required so the recipient can reach the link from the open
   internet (not just your LAN/Tailscale).

### Mint a share (UI)
1. Open the dashboard, click **🔗 Share** in the header
2. Pick "3 days" (or 1/7/14), optionally label it (e.g. *"for J. via SMS"*)
3. Click **Mint link** → copy the URL → text it
4. Manage / revoke from the same dialog

The recipient sees a read-only dashboard with a small banner showing the
expiry. They cannot trigger refreshes, upload data, or use the chat dock
(those are blocked server-side, not just hidden in the UI).

### Mint a share (CLI)
```bash
cd ~/alpine-data
.venv/bin/python share.py --days 3 --label "for J." \
  --host https://dashboard.your-cf-host.com
# prints: https://dashboard.your-cf-host.com/share/<24-char-token>

# list / revoke / prune
.venv/bin/python share.py --list --host https://dashboard.your-cf-host.com
.venv/bin/python share.py --revoke <token-or-full-url>
.venv/bin/python share.py --prune
```

### Cloudflare Tunnel: quick (ephemeral URL)
The fastest way. The public URL changes every time you restart the tunnel,
so this is fine for "share now, dies on next reboot."

```bash
brew install cloudflared

# In one terminal, keep the server running:
HOST=0.0.0.0 .venv/bin/python server.py

# In another terminal, expose it:
cloudflared tunnel --url http://localhost:8765
# →  prints something like
#    Your quick tunnel: https://random-words-1234.trycloudflare.com
```

Mint your share, then text the combined URL:
```
https://random-words-1234.trycloudflare.com/share/<token>
```

When you `Ctrl+C` the cloudflared process (or your Mac reboots), the
public URL stops working. If you re-mint a tunnel later, the token still
works but the host portion changes, so any URL you texted earlier is dead.
For a stable hostname across reboots, see "Named tunnel" below.

### Cloudflare Tunnel: named (stable URL — requires a domain on CF)
If you have any domain on Cloudflare (free plan is fine), you can pin a
stable subdomain like `dashboard.yourdomain.com` to your laptop.

#### Fast path: use the helper scripts
```bash
bash scripts/tunnel-status.sh    # diagnose where you are
bash scripts/tunnel-config.sh    # one-time setup wizard
bash scripts/tunnel-up.sh        # bring tunnel up
```
The wizard handles install detection, login, tunnel creation, config.yml,
and DNS routing — echoes every cloudflared command and prompts for
confirmation before running it. Idempotent: safe to re-run on a
partially-configured machine.

#### Manual recipe (full control)

```bash
# Auth cloudflared to your CF account (opens a browser):
cloudflared tunnel login

# Create a tunnel (one-time):
cloudflared tunnel create dashboard

# Route DNS to it (uses the cert from `tunnel login`):
cloudflared tunnel route dns dashboard dashboard.yourdomain.com

# Config file ~/.cloudflared/config.yml:
cat > ~/.cloudflared/config.yml <<'EOF'
tunnel: dashboard
credentials-file: /Users/btabiado/.cloudflared/<tunnel-uuid>.json
ingress:
  - hostname: dashboard.yourdomain.com
    service: http://localhost:8765
  - service: http_status:404
EOF

# Run it (foreground for now):
cloudflared tunnel run dashboard
```

Now `https://dashboard.yourdomain.com/share/<token>` is stable until you
either:
* revoke the token via the UI / `share.py --revoke`
* wait for the token's natural expiry (3-day default)
* `cloudflared tunnel delete dashboard`

To run it on every boot, install as a launchd service:
```bash
sudo cloudflared service install
```

### Security caveats
* The share URL is read-only at the server level. Even if a viewer crafts
  curl requests, the auth bypass only lets them hit `/`, `/api/data`, and
  `/api/chat`. POST to `/api/refresh`, `/api/upload-csv`, `/api/share`,
  `/api/share/<token>` all require Basic Auth.
* The token is unguessable, but anyone with the link can forward it. If
  you texted it to the wrong person, **revoke** it — don't just wait.
* Chat costs Anthropic API credits per call. Viewers can use chat in
  share mode (it's allowed in `_SHARE_ALLOWED`). If you'd rather block
  that, remove `/api/chat` from `_SHARE_ALLOWED` in `server.py`.
* The token store is `data/shares.json`, gitignored. Don't commit it.

### Revoking
* UI: 🔗 Share → click **Revoke** next to the link
* CLI: `.venv/bin/python share.py --revoke <token-or-url>`
* Wholesale: delete `data/shares.json` (kills *every* active link)

---

## 5. FRED macro data (DXY, SPX, gold, 10Y)

The Trading tab can overlay BTC against macro context: the Broad Dollar
Index (DXY), the S&P 500, London PM gold, the 10-Year Treasury yield, and
M2 money supply. The data comes from the St. Louis Fed's free **FRED API**
— no payment, no rate-limit games, just a self-service key.

Until you paste a key, the Macro section shows a one-line "disabled"
note and skips silently. The rest of the dashboard is unaffected.

### Get a key
1. Visit https://fredaccount.stlouisfed.org/apikeys
2. Sign up (free, instant — email confirmation only)
3. Click **Request API Key**, fill a one-line "what for" reason
   (e.g. "personal trading dashboard")
4. Copy the 32-character hex key it gives you

### Activate it on your Mac
```bash
# Add to your shell startup so it persists across reboots:
echo 'export FRED_API_KEY="<your-32-char-key>"' >> ~/.zprofile
source ~/.zprofile

# Verify it's set:
echo $FRED_API_KEY | head -c 8   # should print 8 chars of your key

# Restart the dashboard server so it picks up the env var:
lsof -ti:8765 | xargs kill -9
cd ~/alpine-data
HOST=0.0.0.0 .venv/bin/python server.py
```

### Verify it took
Trigger a refresh (the floating ⟳ button or `python app.py
--fetch-market`). In the server logs you should see:
```
  FRED macro (DXY/SPX/Gold/10Y/M2)...
```
Then open the **Trading** tab and scroll to the bottom — the
**Macro overlay** card should render BTC, DXY, S&P 500, Gold, and 10Y
yield normalized to 100 at the start of the visible range, plus five
KPI cards with latest values and 1d changes.

If you still see "Macro overlay disabled", the env var didn't make it
into the server's process — re-run `source ~/.zprofile` in the **same
terminal** that you'll launch the server from, then start the server.

### What you get
- **Macro chart** (Trading tab, bottom): BTC vs DXY / SPX / Gold / 10Y,
  with 1M / 3M / 6M / 1Y range selector
- **Macro insights**: surfaced on the dashboard's top insights bar
  when DXY moves ≥1%, the 10Y yield crosses 4.5% or 5%, gold hits a
  30-day high, or the S&P drops ≥2% in a day
- **M2 series**: stored in the payload (`market.fred.m2`) for future use

### Cost
Zero. FRED is a public-good service.

---

## 6. Glassnode (optional — true whale-cohort metrics)

The Whale tab already shows free supply-cohort data scraped from
bitinfocharts.com (whales vs non-whales BTC held over time). For
**richer cohort flow metrics** — number of whale addresses over time,
exchange inflow/outflow in BTC, supply in profit, etc. — wire a
Glassnode API key. The dashboard auto-activates a "Glassnode" KPI
strip on the Whale tab the moment the key is present.

### Get a key
1. Sign up at https://studio.glassnode.com — free tier covers basic
   metrics, **Lite plan (~$30/mo) unlocks the address-cohort series**
   the dashboard uses
2. Account → **API Keys** → create a new key
3. Copy the value (you can see it again later, but you should still
   put it in `~/.zprofile`)

### Activate it on your Mac
```bash
echo 'export GLASSNODE_API_KEY="<your-key>"' >> ~/.zprofile
source ~/.zprofile

# Verify it's set:
echo $GLASSNODE_API_KEY | head -c 12

# Restart the dashboard server so it picks up the env var:
lsof -ti:8765 | xargs kill -9
cd ~/alpine-data
HOST=0.0.0.0 .venv/bin/python server.py
```

### Trigger a refresh
The next auto-refresh (every 30 min) will hit Glassnode. To pull
immediately: click the **↻ Refresh** button in the dashboard header
or `curl -X POST http://127.0.0.1:8765/api/refresh` (auth required).

### What you get when active
A new KPI strip appears at the bottom of the **BTC supply: whales
vs non-whales** card on the Whale tab, with cards showing:

- **Whale addresses (≥1K BTC)** with 7d % change
- **Mega-whale addresses (≥10K BTC)** with 7d % change
- **Transfer volume (BTC)** — total network transfer volume
- **Exchange inflow (BTC)** — flow INTO exchanges (rising = sell pressure)
- **Exchange outflow (BTC)** — flow OUT (rising = accumulation)
- **Supply in profit (%)** — % of supply currently above its cost basis

If your tier doesn't cover a given metric, that card silently skips
(no error). The bitinfocharts cohort chart and free proxies stay live
either way.

### Cost
Tier 1 (free): basic metrics only — most whale cohort series are gated.
Tier 2 ("Lite", ~$30/mo): unlocks address-cohort, exchange-flow series.
Tier 3 ("Advanced", more): adds entity-adjusted and SOPR/MVRV variants.

Cancel anytime — the dashboard falls back gracefully when the key is
removed or expires.

---

## 7. LunarCrush (optional — social sentiment)

The dashboard ships without social-sentiment context out of the box. Wire
a LunarCrush key and the Crypto Overview gains a social-sentiment KPI
strip with Galaxy Score, AltRank, and 24h social volume for the top
50 coins — the cheapest credible social signal available.

### Get a key
1. Sign up at https://lunarcrush.com/developers/api/authentication —
   the **Individual** plan ($24/mo) covers the `coins/list/v1` endpoint
   the dashboard uses; a free trial credit pool is usually included
2. Account → **API Keys** → create a new key
3. Copy the value

### Activate it on your Mac
```bash
echo 'export LUNARCRUSH_API_KEY="<your-key>"' >> ~/.zprofile
source ~/.zprofile

# Verify it's set:
echo $LUNARCRUSH_API_KEY | head -c 12

# Restart the dashboard server so it picks up the env var:
lsof -ti:8765 | xargs kill -9
cd ~/alpine-data
HOST=0.0.0.0 .venv/bin/python server.py
```

### Trigger a refresh
The next auto-refresh (every 30 min) will hit LunarCrush. To pull
immediately: click the **↻ Refresh** button in the dashboard header
or `curl -X POST http://127.0.0.1:8765/api/refresh` (auth required).

### What you get when active
The dashboard renders a **social-sentiment KPI strip on the Crypto
Overview** when the key is present, with per-coin tiles for:

- **Galaxy Score** (0-100 composite of price + social health)
- **AltRank** (rank vs all tracked alts on combined performance)
- **Social volume 24h** (interactions across X, Reddit, news)
- **Sentiment** (bullish/bearish skew)
- **Social dominance** (% of all crypto chatter pointed at this coin)

If your tier doesn't cover a metric or the API returns 4xx, the strip
silently hides (no error). Removing the env var fully disables it.

### Same key also drives the chat dock (MCP)
Setting `LUNARCRUSH_API_KEY` activates **two** features off the one key,
no extra setup needed:

1. **`fetch_market.lunarcrush_snapshot()`** — bulk REST snapshot pulled
   on every auto-refresh (every 30 min) and baked into the dashboard
   payload as `market.lunarcrush.*`. Fast, but can be up to ~30 min
   stale and gated by your tier's rate limits.
2. **Chat dock MCP** — the chat dock attaches LunarCrush's official MCP
   server to its Claude API call, so Claude can make **live tool calls**
   when you ask about social sentiment. Useful when the cached snapshot
   is stale, was rate-limited (HTTP 429), or doesn't cover the coin you
   asked about. See **§8** below.

### Cost
Free tier: limited credit pool, fine for daily refreshes if cached.
Individual ($24/mo): comfortable headroom for personal use.
Builder ($240/mo): production / commercial use.

Cancel anytime — the dashboard falls back gracefully when the key is
removed or expires.

---

## 8. LunarCrush MCP for chat (uses key from §7)

When `LUNARCRUSH_API_KEY` is set, the chat dock wires LunarCrush's
official hosted **MCP server** (`https://lunarcrush.ai/sse?key=...`)
into the Anthropic Messages API call. Claude can then call LunarCrush
tools mid-conversation to fetch fresh social-sentiment data —
**without** waiting for the dashboard's next 30-minute auto-refresh.

### Behaviour
- If `LUNARCRUSH_API_KEY` is **set**: chat dock has live access to
  Galaxy Score, AltRank, trending coins, social-volume series, etc.
  Ask things like *"what's BTC sentiment today?"* or *"which coin is
  trending hardest on Twitter right now?"* and Claude will call the
  MCP tool rather than read the cached snapshot.
- If `LUNARCRUSH_API_KEY` is **unset**: chat dock works exactly as
  before — the system prompt never mentions MCP, no tool calls happen,
  responses fall back to the cached `lunarcrush_snapshot()` data
  embedded in the dashboard payload (or the rule-based fallback if
  `ANTHROPIC_API_KEY` is also unset).

### Override the MCP URL
For a self-hosted MCP proxy (e.g. you want to add auth logging or
caching), set an explicit override and the dashboard will use it
verbatim instead of building one from your API key:
```bash
export LUNARCRUSH_MCP_URL="https://my-proxy.example.com/sse?token=..."
```
Unset it again to fall back to the official endpoint.

### Verify it's wired
POST a chat question; the SSE stream now begins with a small `meta`
frame like:
```
data: {"meta": {"llm_configured": true, "mcp_available": true, "mcp_servers": ["lunarcrush"]}}
```
`mcp_available: true` confirms the server is being passed to
Anthropic. (Backwards-compatible: existing clients ignore the meta
frame and process subsequent `text` frames as before.)

### Cost
The MCP call counts as normal LunarCrush API usage on whichever tier
you're on — same metering as the REST snapshot. Anthropic does **not**
add a surcharge for MCP-routed tool calls.
