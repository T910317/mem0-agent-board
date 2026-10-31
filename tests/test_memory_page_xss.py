"""記憶頁審核分頁：上傳清單裡的代號（originals[].short、ops[].id）要跳脫，不能變成 HTML。

做法：從 ui/memory.html 抽出 esc／refName／refHtml／chip 這幾個純函式，用 node 執行。
本機沒有 node 就跳過。
"""
import json
import os
import re
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NODE = shutil.which("node")


def _js_funcs() -> str:
    src = open(os.path.join(ROOT, "ui", "memory.html"), encoding="utf-8").read()
    parts = [re.search(r"^const esc=.*$", src, re.M).group(0)]
    for name in ("refName", "refHtml", "chip"):
        parts.append(re.search(r"^function " + name + r"\(.*$", src, re.M).group(0))
    return "\n".join(parts)


def _render(tok: str) -> str:
    script = ("let refs=%s;\n%s\nprocess.stdout.write(chip(%s));"
              % (json.dumps({tok: {"name": "x"}}), _js_funcs(), json.dumps(tok)))
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, encoding="utf-8", check=True)
    return out.stdout


@pytest.mark.skipif(not NODE, reason="需要 node")
def test_uploaded_short_code_is_escaped():
    evil = '"><img src=x onerror=alert(1)>'
    h = _render(evil)
    assert "<img" not in h
    assert "&quot;&gt;&lt;img" in h
    assert h.count("<a ") == 1 and h.count("</a>") == 1


@pytest.mark.skipif(not NODE, reason="需要 node")
def test_normal_short_code_unchanged():
    h = _render("abcdef12")
    assert 'href="#ref-abcdef12"' in h and 'data-ref="abcdef12"' in h and "<code>abcdef12</code>" in h
