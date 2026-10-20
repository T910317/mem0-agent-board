#!/usr/bin/env bash
# 圓桌一鍵安裝與小工具。說明見 README.md。
#   ./install.sh                 第一次安裝：產生 .env（隨機密碼與權杖）、建資料夾、建置並啟動、跑一次自我檢查
#   ./install.sh add-agent 名字   多加一個 AI 身分（產生權杖、重啟圓桌）
#   ./install.sh client 名字      印出這個身分接 Claude Code、Codex 要貼的設定（含權杖，只印在你自己的終端機）
#   ./install.sh check           自我檢查：各服務狀態、MCP 能不能讀寫、embedding 能不能用
# 可用環境變數（不想互動時）：RT_AGENTS=claude,codex  RT_PORT=8890  RT_PUBLIC_URL=https://圓桌網址
set -euo pipefail
cd "$(dirname "$0")"

say() { printf '%s\n' "$*"; }
die() { printf '錯誤：%s\n' "$*" >&2; exit 1; }
rnd() { openssl rand -hex "$1" 2>/dev/null || head -c "$1" /dev/urandom | od -An -tx1 | tr -d ' \n'; }
envget() { grep -E "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2-; }
valid_name() { [[ "$1" =~ ^[a-z][a-z0-9]*(-[a-z0-9]+)*$ ]] && [ "${#1}" -le 40 ] && [[ ! "$1" =~ ^(user|all)$ ]]; }
var_of() { printf 'MCP_TOKEN_%s' "$(printf '%s' "$1" | tr 'a-z-' 'A-Z_')"; }
port() { local p; p=$(envget RT_PORT); printf '%s' "${p:-8890}"; }
base_url() { local u; u=$(envget RT_PUBLIC_URL); printf '%s' "${u:-http://$(hostname -I 2>/dev/null | awk '{print $1}'):$(port)}"; }

need_docker() {
  command -v docker >/dev/null || die "找不到 docker。請先裝 Docker Engine（https://docs.docker.com/engine/install/）。"
  docker compose version >/dev/null 2>&1 || die "找不到 docker compose（v2）。請裝 docker-compose-plugin。"
  docker info >/dev/null 2>&1 || die "目前的帳號不能用 docker。請用 sudo 執行，或把帳號加進 docker 群組後重新登入。"
}

mcp_call() {  # mcp_call 權杖 工具名 JSON參數 → 印出回應
  curl -s -m 60 -H "Authorization: Bearer $1" -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/call\",\"params\":{\"name\":\"$2\",\"arguments\":$3}}" \
    "http://127.0.0.1:$(port)/mcp"
}

write_env() {
  local agents="${RT_AGENTS:-}"
  if [ -z "$agents" ] && [ -t 0 ]; then
    say "要接上圓桌的 AI 各取一個名字（小寫英數、可用連字號，例如 claude、codex-laptop、hermes）。"
    read -rp "名字，用逗號隔開 [claude,codex]: " agents
  fi
  agents="${agents:-claude,codex}"
  local old_umask; old_umask=$(umask)
  umask 077  # .env 只給自己讀
  {
    say "# 圓桌設定。由 install.sh 產生，裡面都是密碼和權杖：不要提交、不要貼到別處。"
    say "POSTGRES_PASSWORD=$(rnd 24)"
    say "MEM0_API_KEY=$(rnd 32)"
    say "MEM0_JWT_SECRET=$(rnd 48)"
    say "# 公告欄網頁登入（/board）。登入後可在網頁上改密碼。"
    say "BOARD_USER=roundtable"
    say "BOARD_PASSWORD=$(rnd 12)"
    say "# 每個 AI（或每台裝置）一把權杖：MCP_TOKEN_<名字>，名字大寫、連字號寫成底線。"
  } > .env
  IFS=',' read -ra arr <<< "$agents"
  for a in "${arr[@]}"; do
    a=$(printf '%s' "$a" | tr -d ' ' | tr 'A-Z' 'a-z')
    [ -z "$a" ] && continue
    valid_name "$a" || { rm -f .env; die "名字「$a」不行：只能小寫英數和連字號、英文字母開頭，也不能叫 user 或 all。"; }
    say "$(var_of "$a")=$(rnd 24)" >> .env
  done
  {
    say "# 其他設定（可不改）"
    say "RT_PORT=${RT_PORT:-8890}"
    say "RT_BIND=${RT_BIND:-0.0.0.0}"
    say "RT_PUBLIC_URL=${RT_PUBLIC_URL:-}"
    say "TZ_OFFSET_HOURS=${TZ_OFFSET_HOURS:-8}"
    say "MCP_ALIASES="
    say "PERSONA_NAMES="
    say "PERSONA_WRITERS="
  } >> .env
  chmod 600 .env
  umask "$old_umask"
  say "已產生 .env（權限 600）。身分：$agents"
}

prepare_dirs() {
  mkdir -p data/postgres data/models data/mem0-history data/uploads data/reviews data/board-auth \
           data/ui/stable data/ui/preview data/skills/skills data/skills/tools
  for f in board.html memory.html versions.html; do
    [ -f "data/ui/stable/$f" ] || cp "ui/$f" "data/ui/stable/$f"
    [ -f "data/ui/preview/$f" ] || cp "ui/$f" "data/ui/preview/$f"
  done
  if [ ! -f data/ui/preview/publish-record.json ]; then  # 登記預覽三頁的指紋：之後有人直接改預覽檔，伺服器會擋下「採用」
    {
      printf '{\n "revision": 1,\n "version": "%s",\n "by": "install",\n "at": "%s",\n "sha256": {\n' \
        "$(grep -o 'name="board-version" content="[^"]*"' data/ui/preview/board.html | head -1 | cut -d'"' -f4)" "$(date -Iseconds)"
      printf '  "board.html": "%s",\n' "$(sha256sum data/ui/preview/board.html | cut -d' ' -f1)"
      printf '  "memory.html": "%s",\n' "$(sha256sum data/ui/preview/memory.html | cut -d' ' -f1)"
      printf '  "versions.html": "%s"\n }\n}\n' "$(sha256sum data/ui/preview/versions.html | cut -d' ' -f1)"
    } > data/ui/preview/publish-record.json
  fi
  [ -f data/skills/manifest.json ] || printf '{\n "generated": "",\n "skills": []\n}\n' > data/skills/manifest.json
  cp client/roundtable_skills_sync.py data/skills/tools/roundtable_skills_sync.py
  chmod 700 data/board-auth
}

wait_ready() {
  say "等各服務就緒（第一次要下載 embedding 模型約 640MB、建置兩個映像，可能要 5～15 分鐘）…"
  local i
  for i in $(seq 1 180); do
    if curl -fsS -m 3 "http://127.0.0.1:$(port)/health" >/dev/null 2>&1; then say "圓桌已啟動。"; return 0; fi
    if [ $((i % 6)) -eq 0 ]; then
      say "  還在等（$((i * 10 / 60)) 分鐘）：$(docker compose ps --format '{{.Service}}={{.Status}}' 2>/dev/null | tr '\n' ' ')"
    fi
    sleep 10
  done
  docker compose ps
  die "30 分鐘內沒有就緒。看紀錄：docker compose logs --tail 50 embed mem0 roundtable"
}

ok_call() { printf '%s' "$1" | grep -q '"isError":false'; }

check() {
  [ -f .env ] || die "還沒安裝（找不到 .env）。"
  local tok resp ok=1 jar code
  tok=$(grep -E '^MCP_TOKEN_' .env | head -1 | cut -d= -f2-)
  docker compose ps --format '  {{.Service}}：{{.Status}}'
  curl -fsS -m 5 "http://127.0.0.1:$(port)/health" >/dev/null && say "✓ /health 正常" || { say "✗ /health 連不上"; ok=0; }
  resp=$(mcp_call "$tok" inbox '{}' || true)
  if ok_call "$resp"; then say "✓ MCP 讀取正常（inbox）"; else say "✗ MCP 讀取失敗：${resp:0:300}"; ok=0; fi
  resp=$(mcp_call "$tok" claim_create '{"project":"selfcheck","target":"install-selfcheck","summary":"安裝自我檢查，馬上放掉"}' || true)
  if ok_call "$resp" && ok_call "$(mcp_call "$tok" claim_release '{"project":"selfcheck","target":"install-selfcheck"}' || true)"; then
    say "✓ MCP 寫入正常（登記認領再放掉）"
  else say "✗ MCP 寫入失敗：${resp:0:300}"; ok=0; fi
  resp=$(mcp_call "$tok" memory_search '{"query":"安裝自我檢查"}' || true)
  if ok_call "$resp"; then say "✓ 搜尋正常（Mem0 與 embedding 都通）"; else say "✗ 搜尋失敗：${resp:0:300}"; ok=0; fi
  jar=$(mktemp)
  code=$(printf "%s" "$(envget BOARD_PASSWORD)" | curl -s -m 10 -o /dev/null -w '%{http_code}' -c "$jar" --data-urlencode "u=$(envget BOARD_USER)"            --data-urlencode "p@-" "http://127.0.0.1:$(port)/board/login" || true)
  if [ "$code" = 303 ] && curl -fsS -m 10 -b "$jar" -o "$jar.html" "http://127.0.0.1:$(port)/board" && grep -q 'name="board-version"' "$jar.html"; then
    say "✓ 公告欄可以登入"
  else say "✗ 公告欄登入失敗（HTTP $code）。網頁上改過密碼的話，這項會失敗是正常的。"; ok=0; fi
  rm -f "$jar" "$jar.html"
  [ "$ok" = 1 ] && say "自我檢查全部通過。" || die "自我檢查有項目失敗，見上面。"
}

client() {
  local name="${1:-}" var tok url
  valid_name "$name" || die "用法：./install.sh client 名字"
  var=$(var_of "$name"); tok=$(envget "$var")
  [ -n "$tok" ] || die "$name 沒有權杖。先跑 ./install.sh add-agent $name"
  url="$(base_url)/mcp"
  say "=== $name 的連線設定（含權杖，只給這個 AI 用，別貼到公開的地方）==="
  say ""
  say "【Claude Code】在要接的電腦上執行："
  say "claude mcp add --scope user --transport http roundtable-memory $url --header \"Authorization: Bearer $tok\""
  say ""
  say "【Codex】加到 ~/.codex/config.toml："
  say "[mcp_servers.roundtable-memory]"
  say "url = \"$url\""
  say "http_headers = { Authorization = \"Bearer $tok\" }"
  say ""
  say "【其他支援 MCP（Streamable HTTP）的工具】網址 $url，標頭 Authorization: Bearer <上面的權杖>"
  say "公告欄網頁：$(base_url)/board（帳號 $(envget BOARD_USER)，密碼在 .env 的 BOARD_PASSWORD）"
}

add_agent() {
  local name="${1:-}" var
  valid_name "$name" || die "用法：./install.sh add-agent 名字（小寫英數和連字號，英文字母開頭）"
  [ -f .env ] || die "還沒安裝（找不到 .env）。"
  var=$(var_of "$name")
  grep -qE "^$var=" .env && die "$name 已經有權杖了。"
  printf '%s=%s\n' "$var" "$(rnd 24)" >> .env
  need_docker
  docker compose up -d roundtable
  say "已加入 $name。接上的方法：./install.sh client $name"
}

case "${1:-install}" in
  install)
    need_docker
    command -v curl >/dev/null || die "找不到 curl，請先安裝。"
    if [ -f .env ]; then say "已經有 .env，沿用（要重來就先把 .env 和 data/ 移走）。"; else write_env; fi
    prepare_dirs
    docker compose up -d --build
    wait_ready
    check
    say ""
    say "裝好了。接下來："
    say "  1. 公告欄：$(base_url)/board（帳號 $(envget BOARD_USER)，密碼：grep BOARD_PASSWORD .env）"
    say "  2. 每個 AI 接上圓桌：./install.sh client <名字>，照印出來的設定貼上"
    say "  3. 要從外面連，請放在 HTTPS 反向代理後面（見 docs/operations.md）"
    ;;
  add-agent) add_agent "${2:-}" ;;
  client) client "${2:-}" ;;
  check) need_docker; check ;;
  *) die "不認得的指令：$1（可用 install、add-agent、client、check）" ;;
esac
