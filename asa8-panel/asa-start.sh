#!/bin/bash
# asa-start.sh — avvio engine ASA8 senza shell-quoting nelle unit systemd.
# Uso: asa-start.sh <engine> <dbpath> [port] [logfile]
set -u
ENGINE=${1:?engine mancante}
DB=${2:?db mancante}
PORT=${3:-}
LOG=${4:-/var/log/asa8-$ENGINE.log}
export ASANY8=/opt/sybase/SYBSsa8
export ASANYSH8=/opt/sybase/shared
export LD_LIBRARY_PATH=/opt/sybase/SYBSsa8/lib:/usr/lib
export PATH=/opt/sybase/SYBSsa8/bin:$PATH
if [ "${DB#@}" != "$DB" ]; then
  # File di parametri stile Windows (@DATABASE.txt): contiene -n, -o, -x e lista db
  printf 'Y\n' | exec /opt/sybase/SYBSsa8/bin/dbsrv8 -ud "$DB"
fi
if [ -n "$PORT" ]; then
  printf 'Y\n' | exec /opt/sybase/SYBSsa8/bin/dbsrv8 -ud -n "$ENGINE" -o "$LOG" -x "tcpip(PORT=$PORT)" "$DB"
else
  printf 'Y\n' | exec /opt/sybase/SYBSsa8/bin/dbsrv8 -ud -n "$ENGINE" -o "$LOG" "$DB"
fi
