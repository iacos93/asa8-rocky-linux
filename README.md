# Sybase SQL Anywhere 8.0.2 su Rocky Linux 10 — installazione riproducibile

Installazione completa e collaudata di ASA 8.0.2 EVAL (CD `8002_EVAL`, 2002) su
container Rocky Linux 10 x86_64, con server funzionante: apre DB, rete TCPIP,
SQL completo, `dbinit` crea nuovi database.

## Cosa serve

* Host Proxmox con template `rockylinux-10-default_20251001_amd64.tar.xz`
* Container Rocky 10 fresco: 2 CPU, 2 GB RAM, 20 GB disco, rete bridge (vmbr0)
* Il file `8002_EVAL.iso` (o `8002_EVAL.mdf` + `.mds` → convertire: `brew install mdf2iso && mdf2iso 8002_EVAL.mdf 8002_EVAL.iso`)
* Internet nel container (repo Rocky/EPEL + vault.centos.org)

## Media di installazione

* Fonte primaria: il proprio CD `8002_EVAL` (Sybase SQL Anywhere Studio 8.0.2 EVAL Linux, 2002).
* Copia di preservazione dello **stesso media eval** (no-charge evaluation):
  Internet Archive, collezione Vintage Software —
  `https://archive.org/details/SybaseIAnywhereSQLAnywhereStudioForLinuxV8.0.2Evaluation`
  (358 MB, data pubblicazione 2003-07-31).
* Nota licenze: la 8 è EOL dal 31/01/2008 (la 9 dal 01/2010); per produzione
  serve copertura di licenza (es. runtime Argo). Vedi SAP Community per i
  download ufficiali delle versioni supportate (dalla 12 in poi).

## Download e apertura MDF

Il media è in formato MDF/MDS (Alcohol 120%, ~358 MB: `8002_EVAL.mdf` + `8002_EVAL.mds`).
La ISO che usa lo script si ricava così:

```bash
# 1. download (proprio CD oppure copia di preservazione, vedi sopra)
#    attesi: 8002_EVAL.mdf (357 MB) + 8002_EVAL.mds (pochi KB)

# 2. conversione MDF -> ISO (Mac; su Linux stesso tool: pacchetto mdf2iso)
brew install mdf2iso
mdf2iso 8002_EVAL.mdf 8002_EVAL.iso
# atteso: "Created iso9660: 8002_EVAL.iso" (~299 MB: il calo è normale,
# l'MDF contiene dati raw di settore che la ISO scarta)

# 3. verifica ISO
file 8002_EVAL.iso
# atteso: "ISO 9660 CD-ROM filesystem data '8002_EVAL'"
7z l 8002_EVAL.iso | grep -E "linux/(setup|files.tic|bin/dbinstall)"
# attesi: linux/setup, linux/files.tic (~99 MB), linux/bin/dbinstall
```

Solo a questo punto copiare `8002_EVAL.iso` nel container (vedi Installazione).

## Installazione (3 comandi)

Dalla macchina con la ISO, copiare ISO e script nel container (es. vmid 102):

```bash
scp 8002_EVAL.iso install-asa8.sh root@PROXMOX:/tmp/
ssh root@PROXMOX "pct push 102 /tmp/8002_EVAL.iso /tmp/8002_EVAL.iso && pct push 102 /tmp/install-asa8.sh /tmp/install-asa8.sh"
ssh root@PROXMOX "pct exec 102 -- bash /tmp/install-asa8.sh"
```

Lo script fa tutto da solo (5–10 min) e finisce con un **self-test**:
avvia il DB demo, `dbping` → `Ping server successful`, poi ferma tutto.

## Perché servono i passaggi extra (riassunto diagnosi)

* Rocky 10 non ha pacchetti i686: runtime 32-bit ricostruito a mano —
  glibc EL9 i686 per installer/tool, **glibc 2.3.2** (CentOS 3) per il motore
  (con glibc moderne il server parte ma non apre alcun DB).
* Setup del 2002 patchato (`cut -f,1`, pager `more`/`clear` ombreggiati).
* `dbsrv8`/`dbeng8` wrappati per rieseguirsi sotto glibc 2.3.2
  (serve anche a `dbinit`, che lancia il motore via path assoluto).

## Uso quotidiano (nel container)

```bash
source /opt/sybase/SYBSsa8/bin/asa_config.sh   # poi Y alla licenza eval

# nuovo database (sotto runtime 2.3.2 per via dello spawn interno)
export ASANY8=/opt/sybase/SYBSsa8
export LD_LIBRARY_PATH=/opt/sybase/SYBSsa8/lib:/usr/lib
printf 'Y\nY\nY\n' | /opt/glibc32test23/lib/ld-linux.so.2 \
  --library-path /opt/glibc32test23/lib:/opt/glibc32test23/usr/lib:/opt/sybase/SYBSsa8/lib \
  /opt/sybase/SYBSsa8/bin/dbinit /dati/miodb.db

# avvio server demone (risponde Y da solo alla licenza)
printf 'Y\n' | dbsrv8 -ud -n miosrv -o /tmp/db.log /dati/miodb.db
sleep 12
dbping -c "uid=dba;pwd=sql;eng=miosrv"        # Y alla licenza
dbstop -c "uid=dba;pwd=sql;eng=miosrv"        # stop (Y alla licenza)

# SQL batch (niente curses): scrivere comandi in file
printf 'create table t1 (a integer, b char(20));\ninsert into t1 values (1,'"'"'x'"'"');\ncommit;\nselect * from t1;\n' > /tmp/q.sql
dbisqlc -q -c "uid=dba;pwd=sql;eng=miosrv" /tmp/q.sql   # Y alla licenza
```

Nota: i binari in `/opt/sybase/SYBSsa8/bin/{dbsrv8,dbeng8}` sono wrapper;
gli originali sono in `/root/asa8-orig/`.

## Client Windows / dblocate

* Server in ascolto su TCP+UDP `0.0.0.0:2638`; `dblocate` lo trova se il PC è
  sulla **stessa subnet** (broadcast UDP).
* Da altre reti: connessione diretta `HOST=<ip-container>;PORT=2638`, es.
  `dbisqlc -c "uid=dba;pwd=sql;eng=miosrv;links=tcpip;host=<ip-container>;port=2638"`.
* Serve client ASA versione 8/9 su Windows.

## Limiti noti

* Copia EVAL: banner, nessuna scadenza/blocco osservato (testato: data 2026,
  6 connessioni parallele OK). Per produzione serve copertura di licenza
  (es. runtime Argo); i media full Linux reinstallano con la stessa procedura.
* Tool Java morti (dbisql GUI, Sybase Central: JRE 1.3.1 non parte);
  `dbisqlc` interattivo vuole una pty (`script -qec ...`), in batch `-q` è perfetto.
* `dbisqlc` vuole `libncurses.so.4` (symlink a `.so.6` creato dallo script).

## File

* `install-asa8.sh` — installer automatico (root nel container, ISO in /tmp)
* Log utili: `/tmp/asa8-install.log`, `/tmp/asa8-dnf.log`, log server `-o`
