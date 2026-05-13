# Setup guides

User-action items that can't be installed for you, with copy-paste-ready steps.

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
