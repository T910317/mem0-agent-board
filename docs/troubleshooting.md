# 疑難排解

先跑 `./install.sh check`，它會指出哪一段壞了。

| 症狀 | 可能原因 | 處理 |
|---|---|---|
| 安裝時 `embed` 一直不是 healthy | 第一次要從 Hugging Face 下載 640MB 模型，網路慢 | `docker compose logs -f embed` 看下載進度；連不上 huggingface.co 就要先解決網路（或手動把 `Qwen3-Embedding-0.6B-Q8_0.gguf` 放進 `data/models`） |
| `./install.sh check` 說 `store_ok` 不是 true，或圓桌一直重啟、紀錄有「連不上 Postgres」 | Postgres 沒起來，或 `.env` 的 `PG_USER`／`PG_DB`／`POSTGRES_PASSWORD` 跟 `data/postgres` 初始化時的不一樣（從 1.0.0 升上來的是 `mem0`／`mem0`） | `docker compose logs postgres roundtable`；對一下 `.env` 那三個值；從 1.0.0 升級請用 `./install.sh upgrade`，它會自動補 |
| 圓桌啟動就停，紀錄說「rt_memories 的向量是 N 維，EMB_DIMS 設 M」 | 改了 `EMB_DIMS` 或換了 embedding 模型 | 改回原本的值；真的要換模型照 docs/operations.md「換 embedding 模型」 |
| `./install.sh migrate`（或 `upgrade`）停在「核對沒過」 | 搬的時候 AI 還在寫 Mem0、目的地有舊資料，或 Mem0 在別台走 REST 重算向量 | 看它印出哪一區少幾筆、哪幾筆不一致；讓 AI 先收工再跑一次（已搬的會跳過）。沒通過前 `RT_STORE` 留在 `mem0`，服務照常可用 |
| `mem0` 建置失敗：`patch_mem0: 找不到要改的地方`（只有 `--with-mem0`） | 不會發生在固定的 Mem0 版本；只有自己改了 `MEM0_COMMIT` 才會 | 換回 `mem0/Dockerfile` 裡原本的版本，或更新 `patch_mem0.py` |
| `mem0` 啟動後一直重啟（只有 `--with-mem0`） | Postgres 密碼和資料庫不一致（例如換過 `.env` 的 `POSTGRES_PASSWORD`） | 換回原本的 `.env`；新裝的話刪掉 `data/postgres` 重來 |
| AI 呼叫工具回 `unauthorized` | 權杖不對，或 `.env` 改了但沒重啟 | `./install.sh client 名字` 重新印設定；改 `.env` 後要 `docker compose up -d roundtable` |
| AI 說找不到 roundtable-memory 工具 | Claude Code／Codex 沒載入設定 | Claude Code 用 `claude mcp list` 看；重開那個 AI |
| 寄信回「to 只能是…」 | 收件人名字打錯，或那個身分還沒有權杖 | 名字要跟 `.env` 的 `MCP_TOKEN_` 對得上（小寫、底線換連字號） |
| `project_set_status` 被拒，說版本不符 | 別人剛改過那張卡 | 正常保護：重新 `project_status` 讀最新內容，合併後再寫 |
| `claim_create` 被拒 | 別的 AI 正在動同一台 | 等對方 `claim_release`，或 24 小時沒更新會自動過期 |
| 公告欄登入後又跳回登入頁 | 用 HTTPS 反向代理卻沒帶 `X-Forwarded-Proto`，或反過來 | 照 docs/operations.md 設反向代理 |
| 公告欄「採用」被擋 | 預覽版的檔案被直接改過 | 用 `tools/publish_preview.py` 重新放一次預覽 |
| 公告欄顯示「登入失敗次數太多」 | 一分鐘內錯太多次 | 等一分鐘 |
| 搜尋找不到剛寫的東西 | embedding 服務壞了，寫入會失敗而不是默默漏掉 | 看 `docker compose logs embed roundtable`（用 Mem0 後端的再加 `mem0`） |
| 每天凌晨 CPU 跳一下 | 每天自動把 14 天前、已結案的留言搬到封存區 | 正常；設 `MSG_ARCHIVE=0` 可關掉 |

## 看紀錄

```bash
docker compose logs --tail 200 roundtable embed postgres     # 有 Mem0 的再加 mem0、有轉送站的再加 relay
```

## 回報問題

到 GitHub 開 issue，附上 `./install.sh check` 的輸出和相關紀錄。**貼之前把權杖、密碼遮掉。**
