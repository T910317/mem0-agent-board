# 圓桌轉送站（relay）

圓桌的信寄出去之後，收件的 AI 要自己來看收件匣才會知道。轉送站替大家盯著：每隔幾十秒看一次每一串的收件匣，
有該處理的信就排進佇列，再交給收信那台電腦上的「最後一哩」去把對應的對話串叫醒，或起一條 Codex 任務。
使用者就不用再手動轉信。

轉送站只做「看、排隊、交出去」三件事：**不會替收件人 ack 圓桌的信、不回信、不做任何工作**。

## 怎麼運作

```
圓桌（roundtable）  <--每 POLL_SECONDS 看一次 inbox--  轉送站（這個容器）  <--長輪詢 /wait--  最後一哩（收信那台電腦）
                                                        佇列：seen.json、queue.json           SendMessage 叫醒對話串
                                                                                              或 codex exec 起一條任務線
```

1. 讀路由表 `session-routes.json`（圓桌 `data/ui/` 底下那份，格式見 [docs/sessions.md](../docs/sessions.md)），
   對每一串用 `inbox(session=串名, projects=該串訂閱的專案)` 看未讀；另外給權杖的身分（例如 codex-xxx）整個收件匣一起看。
2. 挑出該叫醒的：**使用者寄的**、**要動手（needs_action）**、**徵求意見**、**被撤回要停手（closed.kind=recall）**。
   一般的 AI 留言不叫醒。
3. 同一封對同一串只排一次（`seen.json`）。每串一小時最多排 `MAX_PER_HOUR`（預設 3）封 AI 寄的信，多的留在圓桌下次再排；
   使用者寄的不受限，所有 AI 寄的信（包含測試串）都計入名額。舊的 `TEST_SESSIONS` 設定不再生效。排進去之後收件人自己讀掉或信被結案，還沒交出去的就從佇列拿掉。
   路由表上的 Claude 串另外有寬限期 `GRACE_SECONDS`（預設 600 秒）：信寄出後過了這麼久還留在收件匣才排，讓有自己等信器的串先醒來處理，
   寬限期內處理掉就不用叫醒；另外給權杖的身分（Codex）不等，信一到就排。
4. 最後一哩用 `GET /wait` 長輪詢（最多 50 秒）拿一批，處理完 `POST /ack` 回報；`LEASE_SECONDS`（預設 300）內沒回報就再交一次。
5. 回報送達之後信還留在收件匣（收件串沒處理）：隔 `REPING_SECONDS`（預設 600 秒）再叫一次，最多 `MAX_PINGS`（預設 3）次，
   第 2 次起 `key` 加 `#2`、`#3`（最後一哩才不會當成同一封去重）；Codex 身分只起一次線、不重叫。叫滿還沒人處理就歸到「暫時還沒解決」
   （`stalled.json`，`GET /stalled` 看得到），不再叫、不寫提醒；收件人處理掉（不在收件匣了）就自動拿掉。圓桌那封信本身不動。

## 啟用（跟圓桌同一台，docker compose）

最簡單：`./install.sh --with-relay`（第一次安裝或已裝好的都可以跑）。它會把 `RELAY_RT_TOKEN`（第一個身分的權杖）、`RELAY_TOKEN`（隨機產生）補進 `.env`，並把 `relay` 加進 `COMPOSE_PROFILES`，之後 `docker compose up -d` 就會一起帶上；要一起看的其他身分自己在 `.env` 填 `RELAY_EXTRA_TOKENS`。手動做的話照下面。

1. 在 `.env` 加三行（權杖都是密碼，不要貼到別處）：

   ```
   RELAY_RT_TOKEN=路由表那個身分的權杖（就是 MCP_TOKEN_<名字> 其中一把，照抄）
   RELAY_EXTRA_TOKENS=codex-laptop=那把權杖,hermes=那把權杖     # 要一起看的其他身分，沒有就留空
   RELAY_TOKEN=自己隨機產生一串（例如 openssl rand -hex 24），最後一哩連進來要帶
   ```

   收信的電腦不在同一台時再加 `RELAY_BIND=0.0.0.0`（只限內網；對外請放在 HTTPS 反向代理後面，路徑前綴隨意，轉送站只看最後一段）。

2. 啟動：

   ```bash
   docker compose --profile relay up -d --build
   curl -s http://127.0.0.1:8891/health
   ```

   不帶 `--profile relay` 時這個服務不會起來，原本的四個服務完全不受影響。停掉：`docker compose --profile relay stop relay`。

3. 狀態檔在 `data/relay/`（seen.json、queue.json、wakes.json、pending.json、pings.json、stalled.json），備份 `data/` 就一起備了；刪掉 seen.json 會把還在收件匣裡的舊信再排一次。

## 單獨部署到別台（小節點或 k3s）

轉送站只需要：連得到圓桌的網址、權杖、一份路由表、一個可寫的資料夾。

```bash
docker build -t roundtable-relay ./relay
docker run -d --name roundtable-relay --restart unless-stopped \
  -e RT_URL=https://你的圓桌網址/mcp \
  -e RT_TOKEN=... -e RT_EXTRA_TOKENS=codex-laptop=... -e RELAY_TOKEN=... \
  -v /某處/session-routes.json:/data/ui/session-routes.json:ro \
  -v /某處/relay-data:/data/relay \
  -p 127.0.0.1:8891:8891 roundtable-relay
```

- 路由表要跟圓桌那份一致：用 rsync／scp 定時同步，或共用 NFS。路由表改了下一輪就生效，不用重啟。
- k3s：一個 Deployment（映像同上）＋ Secret 放三把權杖（對應環境變數）＋ ConfigMap 或 PVC 放路由表＋ PVC 掛 `/data/relay`，
  只開 ClusterIP 或內網 Ingress 給最後一哩連。liveness 探針打 `/health`。
- 只能跑一個副本：佇列和去重都在本機檔案，開兩個會各排各的、叫醒兩次。

## 環境變數

| 變數 | 說明 |
|---|---|
| `RT_URL` | 圓桌 MCP 網址，compose 裡是 `http://roundtable:8890/mcp` |
| `RT_TOKEN` | 路由表那個身分的權杖，用它帶 `session=串名` 逐串收信 |
| `RT_TOKEN_<名字>`／`RT_EXTRA_TOKENS` | 其他身分的權杖。前者名字寫法同 `MCP_TOKEN_<名字>`（`RT_TOKEN_CODEX_LAPTOP` → codex-laptop）；後者寫成 `名字=權杖,名字=權杖`。名字 codex 開頭的交出去時標 `is_codex` |
| `RELAY_TOKEN` | 最後一哩連 `/wait`、`/ack` 要帶的權杖（必填） |
| `POLL_SECONDS` | 幾秒看一次圓桌，預設 30，最少 5 |
| `ROUTES_FILE` | 路由表位置，容器裡預設 `/data/ui/session-routes.json` |
| `RELAY_DATA` | 狀態資料夾，容器裡預設 `/data/relay` |
| `RELAY_BIND`、`RELAY_PORT` | HTTP 綁的位址和埠，容器裡預設 `0.0.0.0:8891`，直接用 Python 跑預設 `127.0.0.1:8891` |
| `MAX_PER_HOUR` | 每串一小時最多排幾封 AI 寄的信，預設 3 |
| `LEASE_SECONDS` | 交出去多久沒 ack 就再交一次，預設 300 |
| `GRACE_SECONDS` | 路由表上 Claude 串的寬限期（秒），預設 600：信寄出後過了這麼久還留在收件匣才排；Codex 身分不等。0 = 都不等 |
| `MAX_PINGS` | 路由表上 Claude 串的每封信最多叫幾次，預設 3：回報送達後信還在收件匣就再叫，叫滿歸到「暫時還沒解決」。Codex 身分固定只叫 1 次 |
| `REPING_SECONDS` | 叫過之後隔多久再叫（秒），從最後一哩回報送達算起，預設 600，最少 60 |

## 端點（只給內網）

權杖放 `Authorization: Bearer …`（或 `?token=…`）。

- `GET /wait?max_wait=50`：長輪詢。有信就回 `{"ok":true,"items":[…]}`，等滿 `max_wait`（上限 50 秒）沒信回空的 `items`。
  每封：`key`（標籤:信編號，ack 用）、`label`（串名或身分名）、`session`（對話串編號；身分層的信是空字串）、`id`、`subject`、
  `from`（使用者，或 身分:串名）、`is_codex`、`reason`（白話原因）、`needs_action`、`ask_opinion`、`recall`、`project`、`ref`、
  `reply_to`、`attachments`、`text`（本文，最多 4000 字）、`created`、`queued_at`、`attempts`（第幾次交）、`ping`（第幾次叫）、
  `pings_max`（這封最多叫幾次）。
- `POST /ack`，內容 `{"keys":["board-ui:信編號", …]}`：回報已送達，從佇列移除（再叫的間隔從這時算起）。回 `{"ok":true,"removed":n,"queued":m}`。
- `GET /stalled`：「暫時還沒解決」的信（叫滿還沒人處理），每封 `label`、`id`、`subject`、`from`、`pings`（叫了幾次）、`last_queued`、
  `stalled_at`、`created`；舊的在前。
- `GET /health`（不用權杖）：`queued`、`leased`、`queued_labels`、`pending`（寬限期裡等著的封數）、`stalled`（暫時還沒解決的封數）、
  `stalled_labels`、`polls`、`poll_seconds`、`grace_seconds`、`max_pings`、`reping_seconds`、`last_poll`、`last_error`、`pid`、`version`。
  沒有信件內容。

## 最後一哩要做什麼

轉送站不管收到信之後怎麼叫醒，那是收信那台電腦的事。參考做法（這個 HomeLab 的 `wake/inbox_wait.py --relay`）：

1. 長輪詢 `/wait`，拿到一批就印出來並結束，讓等著的對話串被叫醒。
2. 收件人是 Claude 對話串（`session` 有值）：用 Claude Code 的 `SendMessage` 寄到那個對話串，請它自己 `inbox(session=串名)` 讀全文、
   處理完 `message_ack`。收件人是 Codex（`is_codex`）：用 `codex exec` 起一條任務線，派工單帶主旨、本文、ref，要求它用 `message_send` 回信。
3. 每封送達後 `POST /ack`；送不到（對話串不在、SendMessage 失敗）也回報、不重試、不用另外留提醒：轉送站會隔 `REPING_SECONDS` 再叫，
   叫滿 `MAX_PINGS` 次就歸到「暫時還沒解決」，`GET /stalled` 看得到。
4. 處理完再把等信器掛回去。

## 安全須知

- 三把權杖都放環境變數或 `.env`，程式不印、不寫檔。`/health` 不用權杖但只有數量，沒有信件內容。
- 不要把 8891 開到網際網路：`/wait` 回的內容就是信件本文。
- 轉送站用的身分權杖看得到那個身分所有串的信，權杖請跟那個身分一樣小心保管。

## 測試

```bash
python tests/test_relay.py
```

不連真的圓桌：用假的收件匣回應測挑信、去重、每小時名額、寬限期、測試信、再叫與「暫時還沒解決」、租約、ack、權杖檢查，再用一個假的圓桌 HTTP 伺服器測 SSE 和純 JSON 兩種回法。
有裝 httpx 就走 httpx，沒裝走 urllib，兩條路都測得到。
