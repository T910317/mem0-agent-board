#!/bin/bash
# Postgres 第一次建立資料庫時執行：另建 mem0_app 放 Mem0 的帳號與設定。
# Mem0 是選配，但這個資料庫一律先建好：之後 ./install.sh --with-mem0 加上 Mem0 就不用重新初始化資料夾。
# 記憶本身都在主資料庫（PG_DB）：圓桌自己的表 rt_memories／rt_history，Mem0 的表 memories。
set -e
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    SELECT 'CREATE DATABASE mem0_app'
    WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'mem0_app')\gexec
EOSQL
