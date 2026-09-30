#!/bin/bash
# install-panel.sh — da eseguire come root dentro il container asa8-prod (102)
set -u
SRC=/tmp/asa8-panel
DST=/opt/asa8-panel
mkdir -p "$DST" /srv/asa /var/log
cp -f "$SRC/panel.py" "$DST/panel.py"
[ -f "$SRC/index.html" ] && cp -f "$SRC/index.html" "$DST/index.html"
for s in asa-start.sh asa-stop.sh; do
  [ -f "$SRC/$s" ] && { cp -f "$SRC/$s" "$DST/$s"; chmod +x "$DST/$s"; }
done
chmod +x "$DST/panel.py"
cp -f "$SRC/asa8-panel.service" /etc/systemd/system/asa8-panel.service
if [ ! -s "$DST/password.txt" ]; then
  tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 16 > "$DST/password.txt"
  chmod 600 "$DST/password.txt"
  echo "Password di accesso al pannello: $(cat "$DST/password.txt")"
  echo "(modificabile in $DST/password.txt, senza riavvio)"
fi
python3 -c "import py_compile; py_compile.compile('$DST/panel.py', doraise=True)" || { echo "ERRORE sintassi python"; exit 1; }
systemctl daemon-reload
systemctl enable --now asa8-panel.service
sleep 2
systemctl is-active asa8-panel.service
ss -tlnp | grep 8181 || ss -tln | grep 8181 || echo "(porta 8181 non ancora in ascolto, vedi journalctl)"
echo "--- test locale ---"
curl -s -m 5 http://127.0.0.1:8181/api/overview | head -c 500; echo
echo "Panel pronto: http://$(hostname -I | awk '{print $1}'):8181"
