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
cd ~/btc-eth-etf-dashboard
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
cd ~/btc-eth-etf-dashboard
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
1. Visit https://github.com/btabiado/btc-eth-etf-dashboard/settings/pages
2. Under **Build and deployment**, set **Source = GitHub Actions**
3. Save

### Push to deploy
The workflow runs on every `git push origin main`. After ~60 sec:
- Check https://github.com/btabiado/btc-eth-etf-dashboard/actions → "pages" workflow → green ✓
- Your dashboard is live at: **https://btabiado.github.io/btc-eth-etf-dashboard/**

### Important caveats
- The Pages version is a **static snapshot** — no live `/api/refresh`,
  no chat dock backend, no `/api/upload-csv`. Charts work, KPI cards work,
  signal scores reflect the moment of generation.
- To publish with **live market data**, run locally before pushing:
  ```bash
  cd ~/btc-eth-etf-dashboard
  HOST=0.0.0.0 .venv/bin/python app.py --fetch-market --no-open
  git add data/market.json data/whale.json
  git commit -m "Refresh data snapshot"
  git push
  ```
- The repo is currently **private**, which means Pages will require a
  GitHub Pro subscription to host. Either flip the repo to public on
  https://github.com/btabiado/btc-eth-etf-dashboard/settings (scroll to
  bottom → Change visibility), or upgrade to Pro.

### Disable
Don't want public Pages? Delete `.github/workflows/pages.yml` and the
workflow stops running.

---

## 4. FRED macro data (DXY, SPX, gold, 10Y)

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
cd ~/btc-eth-etf-dashboard
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
