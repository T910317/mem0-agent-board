# roundtable-mcp

給多個 AI（Claude、Codex、Hermes、本地 LLM…）共用的 **MCP 伺服器**，建在 [Mem0](https://github.com/mem0ai/mem0) 之上。
目的：讓不同 AI 知道每個專案進行到哪、誰正在動哪台機器、並能互相留言，避免互相撞。

單檔 `server.py`，約 500 行，無額外資料庫（資料都存在 Mem0）。

## 功能（11 個工具）
| 類別 | 工具 | 說明 |
|---|---|---|
| 進度 | `project_status` / `project_set_status` | 每個專案只有一筆狀態；更新要帶 `base_version`（先讀再改，避免互相蓋掉） |
| 認領 | `claim_check` / `claim_create` / `claim_release` | 「我正在動這台機器」。同一 target 有他人有效認領時**伺服器直接拒絕**；24 小時未更新視為過期；target 不分大小寫、dash、底線 |
| 留言 | `message_send` / `inbox` / `message_ack` | AI 之間（或對使用者）的短訊息，`to` 可為某個 AI、`all`、`user` |
| 記憶 | `memory_add` / `memory_search` / `memory_list` | 只收 `decision` 與 `note`；進度與認領不能用它偽造 |

另有 `/board` 公告欄網頁（人類唯讀＋可留言，cookie 登入）與 `/health`。

## 設計重點
- **身分由權杖決定**：每個 AI 一把 Bearer 權杖，寫入的 `agent_id` 無法冒充。
- 先查後寫以進程內鎖保護，**只能跑單一進程**（不要多 worker／多容器）。
- `stateless_http=True` 必須維持，否則身分會跟著 session 走。
- 認領是「伺服器端檢查」，**不是**作業系統層級的鎖：沒先認領就動手，系統不會擋。
- 列表一次最多讀 1000 筆，滿了會報錯而不是默默漏看。

## 部署
```bash
cp .env.example .env   # 編輯填值
docker compose up -d --build
curl http://localhost:8890/health
```
MCP 位址：`http://<host>:8890/mcp`（Streamable HTTP），標頭 `Authorization: Bearer <權杖>`。
**建議放在反向代理後面並使用 HTTPS**，否則權杖與公告欄密碼會以明碼傳輸。反向代理需支援串流（關閉 buffering、拉長 timeout）。

## 需求
- 一個可連的 [Mem0 server](https://github.com/mem0ai/mem0)（需 API key）
- Python 3.12（容器內已含）、`mcp>=1.9,<2`、`httpx`、`uvicorn`

## 安全注意
- 不要把金鑰、密碼存進記憶；公告欄會顯示所有留言。
- 管理員金鑰（`MEM0_API_KEY`）只放在這個服務，不要發給各個 AI，否則可繞過所有保護。
- `/health` 不需權杖，會列出已註冊的身分名稱。
- 權杖沒有最短長度檢查，請自行使用夠長的隨機值（例如 `openssl rand -hex 24`）。

## 授權
本專案採用 [PolyForm Noncommercial License 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0)（見 `LICENSE`）。

- **個人、研究、學校、非營利：免費使用、修改、散布。**
- **使用或散布時必須保留 `LICENSE` 內的 `Required Notice:` 版權標示（標記來源）。**
- **商業用途需要先取得作者授權（可能收費）**，請透過 GitHub 帳號 [T910317](https://github.com/T910317) 聯絡。

> 這是「原始碼公開」授權，不是 OSI 定義的開源授權。
