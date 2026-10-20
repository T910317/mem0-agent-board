"""建置 Mem0 映像時套的兩個小修改；原始碼對不上就讓建置失敗，不默默略過。
1. 列表上限 1000 → 10000：圓桌一次要讀整區記憶，1000 筆會默默漏看。
2. 預設設定改讀環境變數：向量維度（EMBED_DIMS）、embedding 服務位址（EMBED_BASE_URL）、模型名稱。
   不改的話第一次開機會用 OpenAI 的 1536 維建表，之後換成 1024 維的本機模型就寫不進去。
"""
import sys

main, req = sys.argv[1], sys.argv[2]


def sub(text, old, new):
    if text.count(old) != 1:
        sys.exit(f"patch_mem0: 找不到要改的地方（或不只一處）：{old[:70]!r}")
    return text.replace(old, new)


t = open(main, encoding="utf-8").read()
t = sub(t, "ALL_MEMORIES_LIMIT = 1000\n", "ALL_MEMORIES_LIMIT = 10000\n")
t = sub(t, '            "collection_name": POSTGRES_COLLECTION_NAME,\n',
        '            "collection_name": POSTGRES_COLLECTION_NAME,\n'
        '            "embedding_model_dims": int(os.environ.get("EMBED_DIMS", "1024")),\n')
t = sub(t, '"embedder": {"provider": "openai", "config": {"api_key": OPENAI_API_KEY, "model": DEFAULT_EMBEDDER_MODEL}},',
        '"embedder": {"provider": "openai", "config": {"api_key": OPENAI_API_KEY, "model": DEFAULT_EMBEDDER_MODEL,\n'
        '        "embedding_dims": int(os.environ.get("EMBED_DIMS", "1024")),\n'
        '        **({"openai_base_url": os.environ["EMBED_BASE_URL"]} if os.environ.get("EMBED_BASE_URL") else {})}},')
open(main, "w", encoding="utf-8").write(t)

r = open(req, encoding="utf-8").read()
r = sub(r, "mem0ai>=0.1.48", "mem0ai")  # 版本由 constraints.txt 固定
open(req, "w", encoding="utf-8").write(r)
print("patch_mem0: 兩處修改都套上了")
