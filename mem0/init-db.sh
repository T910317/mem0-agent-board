#!/bin/bash
# Postgres 第一次建立資料庫時執行：另建 mem0_app 放 Mem0 的帳號與設定（記憶本身放在 mem0 資料庫）。
set -e
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    SELECT 'CREATE DATABASE mem0_app'
    WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'mem0_app')\gexec
EOSQL
