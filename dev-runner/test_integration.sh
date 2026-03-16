#!/usr/bin/env bash
#
# Integration smoke test for dev-runner + Lightpanda browser stack.
#
# Prerequisites:
#   - docker compose up lightpanda dev-runner
#   - Both services healthy
#
# Usage:
#   ./test_integration.sh [DEV_RUNNER_URL]
#
# Default DEV_RUNNER_URL: http://localhost:9000

set -euo pipefail

URL="${1:-http://localhost:9000}"
PASS=0
FAIL=0
APP_ID="smoke-test-$$"

red()   { printf '\033[0;31m%s\033[0m\n' "$*"; }
green() { printf '\033[0;32m%s\033[0m\n' "$*"; }
bold()  { printf '\033[1m%s\033[0m\n' "$*"; }

check() {
    local desc="$1"
    shift
    if "$@" >/dev/null 2>&1; then
        green "  PASS: $desc"
        PASS=$((PASS + 1))
    else
        red "  FAIL: $desc"
        FAIL=$((FAIL + 1))
    fi
}

check_output() {
    local desc="$1"
    local expected="$2"
    local actual="$3"
    if echo "$actual" | grep -q "$expected"; then
        green "  PASS: $desc"
        PASS=$((PASS + 1))
    else
        red "  FAIL: $desc (expected '$expected' in output)"
        FAIL=$((FAIL + 1))
    fi
}

cleanup() {
    curl -sf -X DELETE "$URL/teardown/$APP_ID" >/dev/null 2>&1 || true
}
trap cleanup EXIT

# ────────────────────────────────────────────────────────────────
bold "=== Dev-Runner Integration Smoke Test ==="
bold "    Target: $URL"
echo

# ── 1. Health check ─────────────────────────────────────────────
bold "[1/7] Health check"
HEALTH=$(curl -sf "$URL/health")
check "health endpoint returns 200" test -n "$HEALTH"
check_output "status is ok" '"status":"ok"' "$HEALTH"
check_output "has available_ports" '"available_ports"' "$HEALTH"

# ── 2. Lightpanda healthcheck (via docker) ──────────────────────
bold "[2/7] Lightpanda container health"
if command -v docker >/dev/null 2>&1; then
    LP_STATUS=$(docker inspect --format='{{.State.Health.Status}}' vibe-lightpanda 2>/dev/null || echo "unknown")
    check "lightpanda container is healthy" test "$LP_STATUS" = "healthy"
else
    echo "  SKIP: docker not available (run on host to test container health)"
fi

# ── 3. Deploy a test app ────────────────────────────────────────
bold "[3/7] Deploy test app"

# Create a minimal test app in workspace
WORKSPACE="${WORKSPACE_PATH:-/srv/sftp/workspace/files}"
TEST_APP_DIR="$WORKSPACE/_smoke_test_app"
mkdir -p "$TEST_APP_DIR"

cat > "$TEST_APP_DIR/app.py" << 'PYEOF'
from http.server import HTTPServer, BaseHTTPRequestHandler
import os

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"""<!DOCTYPE html>
<html>
<head><title>Smoke Test</title></head>
<body>
  <h1>Hello Smoke Test</h1>
  <input id="name" type="text" placeholder="Name">
  <button id="greet" onclick="document.getElementById('result').textContent='Hello ' + document.getElementById('name').value">Greet</button>
  <p id="result"></p>
</body>
</html>""")
    def log_message(self, *args):
        pass  # silence

port = int(os.environ.get("PORT", "8100"))
HTTPServer(("0.0.0.0", port), Handler).serve_forever()
PYEOF

DEPLOY_RESP=$(curl -sf -X POST "$URL/deploy" \
    -H "Content-Type: application/json" \
    -d "{\"app_id\":\"$APP_ID\",\"app_dir\":\"_smoke_test_app\",\"command\":\"app.py\",\"runtime\":\"python\"}")

check "deploy returns 200" test -n "$DEPLOY_RESP"
check_output "deploy status is running" '"status":"running"' "$DEPLOY_RESP"
DEPLOYED_PORT=$(echo "$DEPLOY_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['port'])" 2>/dev/null || echo "")
check "deploy returns port" test -n "$DEPLOYED_PORT"
echo "    Deployed on port: $DEPLOYED_PORT"

# Give app a moment to stabilize
sleep 1

# ── 4. Browse endpoint (Lightpanda) ────────────────────────────
bold "[4/7] Browse endpoint (Lightpanda CDP)"
BROWSE_RESP=$(curl -sf -X POST "$URL/browse/$APP_ID" \
    -H "Content-Type: application/json" \
    -d '{"wait_ms": 1000}' 2>&1 || echo "BROWSE_FAILED")

if [ "$BROWSE_RESP" != "BROWSE_FAILED" ]; then
    check_output "browse returns title" '"title":"Smoke Test"' "$BROWSE_RESP"
    check_output "browse returns html" 'Hello Smoke Test' "$BROWSE_RESP"
    check_output "browse returns text" 'Hello Smoke Test' "$BROWSE_RESP"
    # Verify no internal URLs leaked
    check "browse response has no internal URLs" ! echo "$BROWSE_RESP" | grep -q "dev-runner:"
else
    red "  FAIL: browse endpoint returned error"
    FAIL=$((FAIL + 4))
fi

# ── 5. Screenshot endpoint (local Chromium) ─────────────────────
bold "[5/7] Screenshot endpoint (local Chromium)"
SCREENSHOT_RESP=$(curl -sf -X POST "$URL/screenshot/$APP_ID" \
    -H "Content-Type: application/json" \
    -d '{"wait_ms": 1000}' 2>&1 || echo "SCREENSHOT_FAILED")

if [ "$SCREENSHOT_RESP" != "SCREENSHOT_FAILED" ]; then
    check_output "screenshot returns base64 data" '"screenshot_b64"' "$SCREENSHOT_RESP"
    check_output "screenshot returns viewport" '"viewport"' "$SCREENSHOT_RESP"
    check_output "screenshot content type is PNG" '"content_type":"image/png"' "$SCREENSHOT_RESP"
else
    red "  FAIL: screenshot endpoint returned error"
    FAIL=$((FAIL + 3))
fi

# ── 6. Browser test endpoint ────────────────────────────────────
bold "[6/7] Browser test endpoint (Lightpanda CDP)"
BTEST_RESP=$(curl -sf -X POST "$URL/browser-test/$APP_ID" \
    -H "Content-Type: application/json" \
    -d '{
        "steps": [
            {"action": "assert_visible", "selector": "h1", "timeout_ms": 5000},
            {"action": "assert_text", "selector": "h1", "value": "Hello Smoke Test", "timeout_ms": 5000},
            {"action": "wait", "timeout_ms": 500}
        ]
    }' 2>&1 || echo "BTEST_FAILED")

if [ "$BTEST_RESP" != "BTEST_FAILED" ]; then
    check_output "browser test passed" '"passed":true' "$BTEST_RESP"
    check_output "all steps completed" '"steps_completed":3' "$BTEST_RESP"
    check_output "no error" '"error":null' "$BTEST_RESP"
else
    red "  FAIL: browser-test endpoint returned error"
    FAIL=$((FAIL + 3))
fi

# ── 6b. Browser test with click/fill (JS fallback test) ────────
BTEST2_RESP=$(curl -sf -X POST "$URL/browser-test/$APP_ID" \
    -H "Content-Type: application/json" \
    -d '{
        "steps": [
            {"action": "fill", "selector": "#name", "value": "World", "timeout_ms": 5000},
            {"action": "click", "selector": "#greet", "timeout_ms": 5000},
            {"action": "assert_text", "selector": "#result", "value": "Hello World", "timeout_ms": 5000}
        ]
    }' 2>&1 || echo "BTEST2_FAILED")

if [ "$BTEST2_RESP" != "BTEST2_FAILED" ]; then
    check_output "click/fill test passed" '"passed":true' "$BTEST2_RESP"
else
    red "  FAIL: click/fill browser-test returned error"
    red "  Response: $BTEST2_RESP"
    FAIL=$((FAIL + 1))
fi

# ── 6c. Selector validation ────────────────────────────────────
BTEST_BAD=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$URL/browser-test/$APP_ID" \
    -H "Content-Type: application/json" \
    -d '{
        "steps": [{"action": "click", "selector": "div; document.cookie"}]
    }')
check "invalid selector returns 400" test "$BTEST_BAD" = "400"

# ── 7. Teardown ────────────────────────────────────────────────
bold "[7/7] Teardown"
TEARDOWN_RESP=$(curl -sf -X DELETE "$URL/teardown/$APP_ID")
check_output "teardown returns torn_down" '"status":"torn_down"' "$TEARDOWN_RESP"

# Verify port was released
HEALTH_AFTER=$(curl -sf "$URL/health")
check_output "port released after teardown" '"running_apps":0' "$HEALTH_AFTER"

# Clean up test app
rm -rf "$TEST_APP_DIR"

# ── Summary ─────────────────────────────────────────────────────
echo
bold "=== Results ==="
green "  Passed: $PASS"
if [ "$FAIL" -gt 0 ]; then
    red "  Failed: $FAIL"
    exit 1
else
    green "  Failed: 0"
    green "  All tests passed!"
fi
