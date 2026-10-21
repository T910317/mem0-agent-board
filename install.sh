#!/usr/bin/env bash
# 圓桌一鍵安裝與小工具。說明見 README.md。
#   ./install.sh                 第一次安裝：產生 .env（隨機密碼與權杖）、建資料夾、建置並啟動、跑一次自我檢查
#   ./install.sh add-agent 名字   多加一個 AI 身分（產生權杖、重啟圓桌）
#   ./install.sh client 名字      印出這個身分接 Claude Code、Codex 要貼的設定（含權杖，只印在你自己的終端機）
#   ./install.sh check           自我檢查：各服務狀態、儲存層連不連得上、MCP 能不能讀寫、embedding 能不能用
#   ./install.sh migrate [參數]   把 Mem0 裡的記憶搬進圓桌自己的表（rt_memories）並逐區核對；不會自動切換後端（參數見 tools/migrate_store.py）
#   ./install.sh upgrade         1.0.0 升 2.0.0：備份 → 補 .env → 用 Mem0 後端起新版 → 搬資料 → 核對過才把 RT_STORE 切成 pg
#   ./install.sh --with-mem0     安裝時一起裝 Mem0 並用它當儲存層（RT_STORE=mem0，行為同 1.0.0）；已裝好的再跑一次只會補上 Mem0 容器
#   ./install.sh --with-relay    安裝時一起啟用轉送站（選用；已裝好的再跑一次也可以補上，見 relay/README.md）
# 可用環境變數（不想互動時）：RT_AGENTS=claude,codex  RT_PORT=8890  RT_PUBLIC_URL=https://圓桌網址  RT_PROFILES=mem0,relay
set -euo pipefail
cd "$(dirname "$0")"

say() { printf '%s\n' "$*"; }
die() { printf '錯誤：%s\n' "$*" >&2; exit 1; }
rnd() { openssl rand -hex "$1" 2>/dev/null || head -c "$1" /dev/urandom | od -An -tx1 | tr -d ' \n'; }
envget() { grep -E "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2- || true; }   # 沒這個鍵就印空字串；不加 || true 的話 pipefail 會讓 p=$(envget …) 這種賦值把整個腳本停掉（1.0.0 的 .env 沒有 COMPOSE_PROFILES，upgrade 就在這裡默默結束）
envset() { sed -i "/^$1=/d" .env; printf '%s=%s\n' "$1" "$2" >> .env; }        # 改一個值（沒有就加在最後）
envdefault() { grep -qE "^$1=.+" .env || envset "$1" "$2"; }                    # 沒設（或是空的）才補預設值
valid_name() { [[ "$1" =~ ^[a-z][a-z0-9]*(-[a-z0-9]+)*$ ]] && [ "${#1}" -le 40 ] && [[ ! "$1" =~ ^(user|all)$ ]]; }
var_of() { printf 'MCP_TOKEN_%s' "$(printf '%s' "$1" | tr 'a-z-' 'A-Z_')"; }
port() { local p; p=$(envget RT_PORT); printf '%s' "${p:-8890}"; }
base_url() { local u; u=$(envget RT_PUBLIC_URL); printf '%s' "${u:-http://$(hostname -I 2>/dev/null | awk '{print $1}'):$(port)}"; }
store_kind() { local s; s=$(envget RT_STORE); printf '%s' "${s:-pg}"; }        # pg（預設）或 mem0
pg_user() { local u; u=$(envget PG_USER); printf '%s' "${u:-roundtable}"; }
pg_db() { local d; d=$(envget PG_DB); printf '%s' "${d:-roundtable}"; }
has_profile() { case ",$(envget COMPOSE_PROFILES)," in *,"$1",*) return 0 ;; *) return 1 ;; esac; }
add_profile() { has_profile "$1" || { local p; p=$(envget COMPOSE_PROFILES); envset COMPOSE_PROFILES "${p:+$p,}$1"; }; }
mem0_enabled() { has_profile mem0; }

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
    say "# 儲存層：pg＝圓桌直接寫 Postgres 的 rt_memories 表（2.0.0 預設）；mem0＝沿用 Mem0 當儲存層（./install.sh --with-mem0）。"
    say "RT_STORE=$([ "$WITH_MEM0" = 1 ] && printf mem0 || printf pg)"
    say "PG_USER=roundtable"
    say "PG_DB=roundtable"
    say "EMB_DIMS=1024"
    say "# Mem0 的管理金鑰：只有 RT_STORE=mem0 會用到，先產生好，之後要加裝 Mem0 不用再改。"
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
    say "# 選配的容器（mem0、relay），由 --with-mem0、--with-relay 填；docker compose up -d 會自動帶上"
    say "COMPOSE_PROFILES="
  } >> .env
  chmod 600 .env
  umask "$old_umask"
  say "已產生 .env（權限 600）。身分：$agents；儲存層：$(store_kind)"
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
  say "等各服務就緒（第一次要下載 embedding 模型約 640MB、建置映像，可能要 5～15 分鐘）…"
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

health_of() { local cid; cid=$(docker compose ps -q "$1" 2>/dev/null); [ -n "$cid" ] && docker inspect -f '{{.State.Health.Status}}' "$cid" 2>/dev/null || printf 'none'; }

wait_healthy() {  # wait_healthy 服務 分鐘：等那個容器的 healthcheck 變 healthy
  say "等 $1 就緒（最多 $2 分鐘）…"
  local i
  for i in $(seq 1 $(( $2 * 6 ))); do
    [ "$(health_of "$1")" = healthy ] && { say "$1 已就緒。"; return 0; }
    if [ $((i % 6)) -eq 0 ]; then say "  還在等（$((i / 6)) 分鐘）：$1=$(health_of "$1")"; fi
    sleep 10
  done
  docker compose ps
  die "$1 在 $2 分鐘內沒有就緒。看紀錄：docker compose logs --tail 50 $1"
}

ok_call() { printf '%s' "$1" | grep -q '"isError":false'; }

check() {
  [ -f .env ] || die "還沒安裝（找不到 .env）。"
  local tok resp ok=1 jar code kind n
  tok=$(grep -E '^MCP_TOKEN_' .env | head -1 | cut -d= -f2-)
  kind=$(store_kind)
  docker compose ps --format '  {{.Service}}：{{.Status}}'
  resp=$(curl -fsS -m 5 "http://127.0.0.1:$(port)/health" || true)
  if [ -n "$resp" ]; then say "✓ /health 正常"; else say "✗ /health 連不上"; ok=0; fi
  if printf '%s' "$resp" | grep -q "\"store\": *\"$kind\""; then say "✓ 儲存層是 $kind（跟 .env 的 RT_STORE 一致）"
  else say "✗ /health 回的儲存層跟 .env 的 RT_STORE=$kind 不一致（改過 .env 要 docker compose up -d roundtable）：${resp:0:200}"; ok=0; fi
  if printf '%s' "$resp" | grep -q '"store_ok": *true'; then say "✓ 儲存層連得上（store_ok）"; else say "✗ 儲存層連不上（store_ok 不是 true）"; ok=0; fi
  case "$kind" in
    pg)
      if n=$(docker compose exec -T postgres psql -U "$(pg_user)" -d "$(pg_db)" -Atc "select count(*) from rt_memories" 2>/dev/null); then
        say "✓ Postgres 可以查 rt_memories（$n 筆）"
      else say "✗ Postgres 查不到 rt_memories（PG_USER=$(pg_user)、PG_DB=$(pg_db)；看 docker compose logs --tail 30 postgres roundtable）"; ok=0; fi
      ;;
    mem0)
      if docker compose exec -T mem0 python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/docs', timeout=4)" >/dev/null 2>&1; then
        say "✓ Mem0 /docs 正常"
      else say "✗ Mem0 /docs 連不上（看 docker compose logs --tail 30 mem0）"; ok=0; fi
      ;;
    *) say "✗ RT_STORE 只能是 pg 或 mem0，現在是 $kind"; ok=0 ;;
  esac
  resp=$(mcp_call "$tok" inbox '{}' || true)
  if ok_call "$resp"; then say "✓ MCP 讀取正常（inbox）"; else say "✗ MCP 讀取失敗：${resp:0:300}"; ok=0; fi
  resp=$(mcp_call "$tok" claim_create '{"project":"selfcheck","target":"install-selfcheck","summary":"安裝自我檢查，馬上放掉"}' || true)
  if ok_call "$resp" && ok_call "$(mcp_call "$tok" claim_release '{"project":"selfcheck","target":"install-selfcheck"}' || true)"; then
    say "✓ MCP 寫入正常（登記認領再放掉）"
  else say "✗ MCP 寫入失敗：${resp:0:300}"; ok=0; fi
  resp=$(mcp_call "$tok" memory_search '{"query":"安裝自我檢查"}' || true)
  if ok_call "$resp"; then say "✓ 搜尋正常（儲存層與 embedding 都通）"; else say "✗ 搜尋失敗：${resp:0:300}"; ok=0; fi
  jar=$(mktemp)
  code=$(printf "%s" "$(envget BOARD_PASSWORD)" | curl -s -m 10 -o /dev/null -w '%{http_code}' -c "$jar" --data-urlencode "u=$(envget BOARD_USER)"            --data-urlencode "p@-" "http://127.0.0.1:$(port)/board/login" || true)
  if [ "$code" = 303 ] && curl -fsS -m 10 -b "$jar" -o "$jar.html" "http://127.0.0.1:$(port)/board" && grep -q 'name="board-version"' "$jar.html"; then
    say "✓ 公告欄可以登入"
  else say "✗ 公告欄登入失敗（HTTP $code）。網頁上改過密碼的話，這項會失敗是正常的。"; ok=0; fi
  rm -f "$jar" "$jar.html"
  if relay_enabled; then
    local rp; rp=$(envget RELAY_PORT); rp="${rp:-8891}"
    if curl -fsS -m 5 "http://127.0.0.1:$rp/health" >/dev/null 2>&1; then say "✓ 轉送站 /health 正常"; else say "✗ 轉送站 /health 連不上（看 docker compose logs --tail 30 relay）"; ok=0; fi
  fi
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

# Mem0（選配）：--with-mem0 或 RT_PROFILES=mem0。把 mem0 加進 COMPOSE_PROFILES、補 Mem0 的金鑰；
# .env 還沒決定儲存層（1.0.0 升上來）就設 RT_STORE=mem0；已經是 pg 的不改（圓桌自己表裡的資料不會跟著搬到 Mem0）。
enable_mem0() {
  [ -f .env ] || die "還沒安裝（找不到 .env）。"
  envdefault MEM0_API_KEY "$(rnd 32)"
  envdefault MEM0_JWT_SECRET "$(rnd 48)"
  envdefault EMB_DIMS 1024
  add_profile mem0
  mkdir -p data/mem0-history
  if [ -z "$(envget RT_STORE)" ]; then envset RT_STORE mem0; fi
  if [ "$(store_kind)" = mem0 ]; then
    say "已啟用 Mem0（profile mem0）並用它當儲存層（RT_STORE=mem0）：記憶放在 Mem0 的 memories 表，行為跟 1.0.0 一樣。"
  else
    say "已啟用 Mem0 容器（profile mem0）。儲存層還是 pg；要改用 Mem0 當儲存層，把 .env 的 RT_STORE 改成 mem0 再 docker compose up -d roundtable（rt_memories 裡的資料不會跟著過去）。"
  fi
}

# 搬資料：Mem0 的 memories 表（或走 REST）→ 圓桌的 rt_memories／rt_history，在 roundtable 的映像裡跑 tools/migrate_store.py，
# 核對（筆數、抽樣逐欄、搜尋前 10 筆）不過就以非 0 結束。不會改 RT_STORE。
migrate() {
  [ -f .env ] || die "還沒安裝（找不到 .env）。"
  need_docker
  [ -f tools/migrate_store.py ] || die "找不到 tools/migrate_store.py。"
  if mem0_enabled; then
    docker compose up -d mem0          # 連帶把 postgres、embed 帶起來
    wait_healthy mem0 30
  elif [ -n "$(envget MEM0_URL)" ]; then
    say "COMPOSE_PROFILES 沒有 mem0：用 .env 的 MEM0_URL 走 REST 搬（來源在別台）。"
    docker compose up -d postgres embed
    wait_healthy postgres 5
    wait_healthy embed 30
  else
    die "沒有來源可搬：要嘛 .env 的 COMPOSE_PROFILES 含 mem0（./install.sh --with-mem0，Mem0 在同一台），要嘛在 .env 設 MEM0_URL、MEM0_API_KEY 指到別台的 Mem0 走 REST。"
  fi
  say "開始搬資料（在 roundtable 的映像裡跑 tools/migrate_store.py）…"
  docker compose run --rm --no-deps -T \
    -v "$PWD/tools:/tools:ro" -v "$PWD/data/mem0-history:/history:ro" \
    -e MEM0_HISTORY_DB=/history/history.db \
    roundtable python /tools/migrate_store.py "$@"
}

# 1.0.0 → 2.0.0：先用 Mem0 後端把新版跑起來（行為同 1.0.0），搬資料核對過了才切 pg。中途任何一步失敗都停在 Mem0 後端，服務照常。
upgrade() {
  [ -f .env ] || die "找不到 .env：upgrade 是給已經裝好 1.0.0 的資料夾用的（新裝直接 ./install.sh）。"
  [ -d data/postgres ] || die "找不到 data/postgres，看起來不是裝好的資料夾。"
  need_docker
  if [ "$(envget RT_STORE)" = pg ]; then die "RT_STORE 已經是 pg，不用升級。只想再搬一次資料用 ./install.sh migrate。"; fi
  # 1.0.0 的資料庫是用 mem0／mem0 初始化的（資料夾建好後改 POSTGRES_USER／POSTGRES_DB 不會生效），所以沿用
  envdefault PG_USER mem0
  envdefault PG_DB mem0
  enable_mem0                                  # 補 RT_STORE=mem0、EMB_DIMS、COMPOSE_PROFILES=mem0
  local stamp; stamp=$(date +%Y%m%d-%H%M%S)
  if [ "${RT_SKIP_BACKUP:-0}" != 1 ]; then
    say "備份到 backup/（資料庫用 pg_dumpall，其他檔案打包）…"
    mkdir -p backup && chmod 700 backup          # 備份裡有密碼和權杖：資料夾 700、檔案 600，只有執行的帳號讀得到
    docker compose up -d postgres
    wait_healthy postgres 5
    docker compose exec -T postgres pg_dumpall -U "$(pg_user)" | gzip > "backup/pg-$stamp.sql.gz"
    chmod 600 "backup/pg-$stamp.sql.gz"
    [ -s "backup/pg-$stamp.sql.gz" ] || die "pg_dumpall 沒有輸出，先停下來查 docker compose logs postgres"
    tar --exclude=data/models --exclude=data/postgres -czf "backup/files-$stamp.tgz" .env data \
      || { rm -f "backup/files-$stamp.tgz"; die "打包 data/ 失敗（有些資料夾只有 root 讀得到）：用 sudo 跑 ./install.sh upgrade，或先照 docs/operations.md 自己備份再加 RT_SKIP_BACKUP=1 重跑。"; }   # 失敗的半成品不留，免得被當成備份
    chmod 600 "backup/files-$stamp.tgz"
    say "已備份：backup/pg-$stamp.sql.gz、backup/files-$stamp.tgz（裡面有密碼和權杖，放好）。"
  fi
  prepare_dirs
  say "用 Mem0 後端建置並啟動 2.0.0（這一步之後的行為跟 1.0.0 一樣）…"
  docker compose build
  docker compose up -d mem0
  wait_healthy mem0 30
  docker compose up -d
  wait_ready
  check
  say ""
  migrate || die "搬資料或核對沒過：RT_STORE 還是 mem0，服務照常用 Mem0。照上面的輸出處理後再跑 ./install.sh migrate，通過了再把 .env 的 RT_STORE 改成 pg 並 docker compose up -d roundtable。"
  say ""
  say "核對通過，把儲存層切成 pg…"
  envset RT_STORE pg
  docker compose up -d roundtable
  wait_ready
  check
  say ""
  say "升級完成：圓桌現在直接讀寫 Postgres 的 rt_memories。Mem0 容器先留著（原本的 memories 表沒動）。"
  say "  - 用一週沒問題後：把 .env 的 COMPOSE_PROFILES 裡的 mem0 拿掉，docker compose --profile mem0 rm -sf mem0（memories 表留著不礙事）。"
  say "  - 要退回去：把 .env 的 RT_STORE 改回 mem0，docker compose up -d roundtable（切 pg 之後新寫的資料不會回到 Mem0）。"
}

# 轉送站（選用）：--with-relay 或 RT_PROFILES=relay。只補 .env 的轉送站設定並把 relay 加進 COMPOSE_PROFILES，
# 之後 docker compose up -d 就會一起帶上；其他服務不受影響。細節見 relay/README.md。
relay_enabled() { case ",$(envget COMPOSE_PROFILES)," in *,relay,*) return 0 ;; *) return 1 ;; esac; }

enable_relay() {
  [ -f .env ] || die "還沒安裝（找不到 .env）。"
  local first p
  first=$(grep -E '^MCP_TOKEN_' .env | head -1 | cut -d= -f2-)
  [ -n "$first" ] || die "找不到任何 MCP_TOKEN_，轉送站沒有身分可以看收件匣。"
  grep -qE '^RELAY_RT_TOKEN=.+' .env || { sed -i '/^RELAY_RT_TOKEN=/d' .env; printf 'RELAY_RT_TOKEN=%s
' "$first" >> .env; }
  grep -qE '^RELAY_TOKEN=.+' .env || { sed -i '/^RELAY_TOKEN=/d' .env; printf 'RELAY_TOKEN=%s
' "$(rnd 24)" >> .env; }
  grep -qE '^RELAY_EXTRA_TOKENS=' .env || printf 'RELAY_EXTRA_TOKENS=
' >> .env
  if ! relay_enabled; then
    p=$(envget COMPOSE_PROFILES)
    sed -i '/^COMPOSE_PROFILES=/d' .env
    printf 'COMPOSE_PROFILES=%s
' "${p:+$p,}relay" >> .env
  fi
  mkdir -p data/relay
  say "已啟用轉送站（profile relay）：它看 data/ui/session-routes.json 裡每一串的收件匣；收信端怎麼掛見 relay/README.md。"
}

WITH_RELAY=0; WITH_MEM0=0; _args=()
for _a in "$@"; do case "$_a" in --with-relay) WITH_RELAY=1 ;; --with-mem0) WITH_MEM0=1 ;; *) _args+=("$_a") ;; esac; done
set -- "${_args[@]+"${_args[@]}"}"
case ",${RT_PROFILES:-}," in *,relay,*) WITH_RELAY=1 ;; esac
case ",${RT_PROFILES:-}," in *,mem0,*) WITH_MEM0=1 ;; esac

case "${1:-install}" in
  install)
    need_docker
    command -v curl >/dev/null || die "找不到 curl，請先安裝。"
    if [ -f .env ]; then say "已經有 .env，沿用（要重來就先把 .env 和 data/ 移走）。"; else write_env; fi
    if [ "$WITH_MEM0" = 1 ]; then enable_mem0; fi
    if [ "$WITH_RELAY" = 1 ]; then enable_relay; fi
    prepare_dirs
    docker compose build
    if mem0_enabled && [ "$(store_kind)" = mem0 ]; then
      docker compose up -d mem0          # compose 做不到「只在 RT_STORE=mem0 時等 mem0」：這裡先等它健康再起圓桌
      wait_healthy mem0 30
    fi
    docker compose up -d
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
  migrate) shift; migrate "$@" ;;
  upgrade) upgrade ;;
  *) die "不認得的指令：$1（可用 install、add-agent、client、check、migrate、upgrade）" ;;
esac
