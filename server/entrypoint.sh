#!/bin/sh
# 公告欄改密碼的功能要求存密碼的資料夾屬於伺服器本身的帳號、別人不能讀（見 BoardPasswordStore.available）。
# 資料夾是從主機掛進來的，主機上建的時候屬於一般帳號，所以每次啟動先把它設好。
d=$(dirname "${BOARD_AUTH_FILE:-/board-auth/credential.json}")
if [ -d "$d" ]; then chown "$(id -u):$(id -g)" "$d" && chmod 700 "$d"; fi
exec python /app/server.py
