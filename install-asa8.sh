#!/bin/bash
# ============================================================================
# install-asa8.sh — Sybase SQL Anywhere 8.0.2 EVAL su Rocky Linux 10 (x86_64)
# Eseguire come root dentro un container Rocky 10 fresco.
# Prerequisito: /tmp/8002_EVAL.iso presente nel container.
# Durata tipica: 5-10 minuti. Log setup: /tmp/asa8-install.log
# ============================================================================
set -u
ISO=/tmp/8002_EVAL.iso
DIST=/tmp/sa8full/linux
ASA=/opt/sybase/SYBSsa8
G32T=/opt/glibc32test
G23=/opt/glibc32test23
BK=/root/asa8-orig
LOG=/tmp/asa8-install.log

fail() { echo "ERRORE: $1" >&2; echo "Ultime righe di $LOG:" >&2; tail -n 15 "$LOG" 2>/dev/null >&2; exit 1; }
info() { echo "==> $1"; }

[ "$(id -u)" = "0" ] || { echo "Eseguire come root"; exit 1; }
[ -f "$ISO" ] || { echo "Manca $ISO: copiarla prima nel container"; exit 1; }
[ "$(uname -m)" = "x86_64" ] || { echo "Serve x86_64"; exit 1; }

info "1/8 pacchetti base"
dnf -y install epel-release >/tmp/asa8-dnf.log 2>&1
dnf -y install tar ncurses bsdtar ncurses-compat-libs libnsl libXtst libX11 libXext libXi >>/tmp/asa8-dnf.log 2>&1 \
  || fail "dnf install base (vedi /tmp/asa8-dnf.log nel container)"
command -v bsdtar >/dev/null || fail "manca bsdtar"
SZIP=bsdtar

info "2/8 runtime 32-bit EL9 (per installer e tool)"
mkdir -p /root/i686 "$G32T"
dnf install -y epel-release >/dev/null 2>&1
dnf download --releasever=9 --destdir=/root/i686 \
  glibc.i686 libstdc++.i686 ncurses-libs.i686 libnsl.i686 libxcrypt.i686 zlib.i686 \
  >>/tmp/asa8-dnf.log 2>&1 || fail "download i686 EL9"
rpm -i --root "$G32T" --nodeps --noscripts /root/i686/*.rpm 2>/dev/null
[ -f "$G32T/usr/lib/ld-linux.so.2" ] || fail "estrazione glibc EL9"
cp -n "$G32T/usr/lib/ld-linux.so.2" /lib/ld-linux.so.2
# shellcheck disable=SC2035
cp -n "$G32T"/usr/lib/*.so* /usr/lib/ 2>/dev/null
"$G32T/usr/lib/ld-linux.so.2" --version >/dev/null || fail "loader EL9"

info "3/8 runtime 32-bit glibc 2.3.2 (per il motore dbsrv8/dbeng8)"
mkdir -p "$G23"
curl -sL --max-time 120 -o /root/glibc23.i686.rpm \
  https://vault.centos.org/3.9/os/i386/RedHat/RPMS/glibc-2.3.2-95.50.i686.rpm || fail "download glibc 2.3.2"
curl -sL --max-time 120 -o /root/libstd23.i386.rpm \
  https://vault.centos.org/3.9/os/i386/RedHat/RPMS/libstdc++-3.2.3-59.i386.rpm || fail "download libstdc++ 3.2.3"
rpm -i --root "$G23" --nodeps --noscripts /root/glibc23.i686.rpm /root/libstd23.i386.rpm 2>/dev/null
[ -f "$G23/lib/ld-linux.so.2" ] || fail "estrazione glibc 2.3.2"

info "4/8 estrazione ISO"
mkdir -p /tmp/sa8full
bsdtar -xf "$ISO" -C /tmp/sa8full linux >>"$LOG" 2>&1
bsdtar -xf "$ISO" -C /tmp/sa8full linux/files.tic >>"$LOG" 2>&1 || fail "estrazione ISO"
[ -x "$DIST/setup" ] && [ -f "$DIST/files.tic" ] || fail "file setup/files.tic mancanti"

info "5/8 patch setup 2002 per coreutils moderni"
cp -p "$DIST/setup" "$DIST/setup.orig"
sed -i 's/cut -d" " -f,/cut -d" " -f/g; s/cut -d\. -f,/cut -d. -f/g' "$DIST/setup"
printf '#!/bin/sh\ncat "$@"\n' > "$DIST/bin/more"
printf '#!/bin/sh\nexit 0\n' > "$DIST/bin/clear"
chmod +x "$DIST/setup" "$DIST/bin/dbinstall" "$DIST/bin/more" "$DIST/bin/clear"

info "6/8 setup Sybase (risposte automatiche: tutto, default, sì)"
printf '\nY\nA\nS\n\n\nY\n' | "$DIST/setup" >"$LOG" 2>&1
[ -x "$ASA/bin/dbsrv8" ] || fail "setup incompleto"
ln -sf libncurses.so.6 /usr/lib/libncurses.so.4 2>/dev/null
chmod u+w "$ASA/asademo.db" 2>/dev/null

info "7/8 wrapper runtime 2.3.2 per dbsrv8/dbeng8"
mkdir -p "$BK"
for b in dbsrv8 dbeng8; do
  [ -f "$BK/$b.real232" ] || cp -p "$ASA/bin/$b" "$BK/$b.real232"
  cat > "$ASA/bin/$b" <<EOF
#!/bin/bash
exec -a $b $G23/lib/ld-linux.so.2 --library-path $G23/lib:$G23/usr/lib:$ASA/lib $BK/$b.real232 "\$@"
EOF
  chmod +x "$ASA/bin/$b"
done

info "8/8 self-test: avvio demo + ping + stop"
export ASANY8="$ASA"
export PATH="$ASA/bin:$PATH"
export LD_LIBRARY_PATH="$ASA/lib:/usr/lib"
printf 'Y\n' | timeout 60 dbsrv8 -ud -n asademo -o /tmp/asa8-selftest.log "$ASA/asademo.db" >/dev/null 2>&1
sleep 12
printf 'Y\n' | timeout 30 dbping -c "uid=dba;pwd=sql;eng=asademo" > /tmp/asa8-ping.log 2>&1
if grep -q "Ping server successful" /tmp/asa8-ping.log; then
  echo "SELF-TEST OK: server avviato e raggiungibile"
else
  tail -n 5 /tmp/asa8-ping.log >&2; fail "self-test ping fallito"
fi
printf 'Y\n' | timeout 30 dbstop -c "uid=dba;pwd=sql;eng=asademo" >/dev/null 2>&1
sleep 3

echo
echo "INSTALLAZIONE COMPLETATA in $ASA"
echo "Avvio manuale:  source $ASA/bin/asa_config.sh  (poi Y alla licenza)"
echo "  printf 'Y\n' | dbsrv8 -ud -n miodb -o /tmp/db.log /percorso/miodb.db"
echo "  dbping -c \"uid=dba;pwd=sql;eng=miodb\"   (Y alla licenza)"
echo "Nuovo DB:       dbinit /percorso/nuovo.db   (sotto glibc 2.3.2: vedi README)"
echo "Dettagli e Windows-client nel README."
