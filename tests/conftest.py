"""測試共用設定：在匯入 server.py 之前先把環境變數設成測試值。

server.py 匯入時就會讀環境變數（MEM0_API_KEY、MCP_TOKEN_* 等），
所以這裡先清掉本機真正的 MCP_TOKEN_*，免得測試用到或印出真的權杖。
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

for _k in [k for k in os.environ if k.startswith("MCP_TOKEN_")]:
    del os.environ[_k]

_tmp = tempfile.mkdtemp(prefix="rt-test-", dir=os.environ.get("RT_TEST_TMP") or None)
os.environ.update({
    "MEM0_API_KEY": "test-mem0-key",
    "MCP_TOKEN_CLAUDE": "test-token-claude-0123456789abcdef0123456789",
    "MCP_TOKEN_CODEX": "test-token-codex-0123456789abcdef01234567890",
    "BOARD_USER": "tester",
    "BOARD_PASSWORD": "test-board-password-123",
    "BOARD_AUTH_FILE": "",
    "UI_DIR": os.path.join(_tmp, "ui"),
    "UPLOAD_DIR": os.path.join(_tmp, "uploads"),
    "REVIEW_DIR": os.path.join(_tmp, "reviews"),
    "SKILLS_ROOT": os.path.join(_tmp, "skills"),
    "MSG_ARCHIVE": "0",
})
