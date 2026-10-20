"""從 server/*.py 的 @mcp.tool() 函式抽出名稱、參數和說明，產生 docs/tools.md。改了工具就重跑：python3 tools/gen_tools_doc.py"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GROUPS = [("進度與白板", ("project_",)), ("認領（誰在動哪台機器）", ("claim_",)), ("留言與收件匣", ("message_", "inbox")),
          ("共用記憶", ("memory_",)), ("經歷區（個人資料，和共用記憶分開）", ("experience_",)), ("附件", ("attachment_",)),
          ("共用技能庫", ("skill_",)), ("人格備份（預設關閉）", ("persona_",))]


def tools(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                "tool" in ast.unparse(d) for d in node.decorator_list):
            a = node.args
            defaults = [None] * (len(a.args) - len(a.defaults)) + list(a.defaults)
            params = []
            for arg, d in zip(a.args, defaults):
                t = ast.unparse(arg.annotation) if arg.annotation else ""
                params.append(f"`{arg.arg}`" + (f"（{t}" + (f"，預設 {ast.unparse(d)}" if d is not None else "，必填") + "）" if t else ""))
            yield node.name, params, (ast.get_docstring(node) or "").strip()


rows = [t for f in ("server/server.py", "server/experience_store.py") for t in tools(ROOT / f)]
out = ["# 工具一覽", "", f"圓桌一共有 {len(rows)} 個 MCP 工具。這份由 `tools/gen_tools_doc.py` 從原始碼產生，說明文字就是 AI 看到的工具描述。", ""]
done = set()
for title, prefixes in GROUPS + [("其他", ("",))]:
    part = [r for r in rows if r[0].startswith(prefixes) and r[0] not in done]
    if not part:
        continue
    out += [f"## {title}", ""]
    for name, params, doc in part:
        done.add(name)
        out += [f"### `{name}`", "", "參數：" + ("、".join(params) if params else "無"), "", doc.replace("\n    ", "\n"), ""]
(ROOT / "docs" / "tools.md").write_text("\n".join(out), encoding="utf-8", newline="\n")
print(f"docs/tools.md：{len(rows)} 個工具")
