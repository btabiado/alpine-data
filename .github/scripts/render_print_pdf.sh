#!/usr/bin/env bash
# Render a static page's one-page print layout (?print) to a PDF with headless Chrome.
# Used for the Download PDF buttons on /crypto-odds/ and /crypto-scorecard/.
#   usage: render_print_pdf.sh <dir containing index.html and its JSON> <output.pdf>
set -euo pipefail
dir=$(cd "$1" && pwd); out=$2; port=${PORT:-8791}
CHROME=${CHROME:-$(command -v google-chrome || command -v chromium || echo "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")}
python3 -m http.server "$port" --bind 127.0.0.1 --directory "$dir" >/dev/null 2>&1 &
srv=$!
trap 'kill $srv 2>/dev/null' EXIT
sleep 1
"$CHROME" --headless=new --disable-gpu --no-sandbox --hide-scrollbars --no-pdf-header-footer \
  --window-size=1100,1600 --virtual-time-budget=10000 --print-to-pdf="$out" "http://127.0.0.1:$port/?print" >/dev/null 2>&1
test -s "$out"
