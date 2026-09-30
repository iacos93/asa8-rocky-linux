#!/bin/bash
# asa-stop.sh — stop engine ASA8 senza shell-quoting nelle unit systemd.
# Uso: asa-stop.sh <engine> [user] [password]
set -u
ENGINE=${1:?engine mancante}
USER=${2:-dba}
PWD=${3:-sql}
export ASANY8=/opt/sybase/SYBSsa8
export ASANYSH8=/opt/sybase/shared
export LD_LIBRARY_PATH=/opt/sybase/SYBSsa8/lib:/usr/lib
export PATH=/opt/sybase/SYBSsa8/bin:$PATH
printf 'Y\n' | exec /opt/sybase/SYBSsa8/bin/dbstop -c "uid=$USER;pwd=$PWD;eng=$ENGINE"
