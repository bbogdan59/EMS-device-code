# Operare Pi: actualizări, resurse, ce rămâne doar pe hardware real (issue #4)

## Actualizări verificate cu rollback

Update-urile pot porni cu monitorizarea activă. `run.sh` creează un director
`releases/bootstrap.<id>` nou pentru fiecare instalare; updaterul semnat folosește
`releases/<version>`. Virtualenv-ul se construiește direct la calea definitivă:
mutarea lui după instalare ar invalida shebang-urile scripturilor Python.

Ambele fluxuri iau `/opt/ems-device/.update.lock`, separat de `agent.lock`.
Descărcarea și build-ul lasă monitorizarea activă. `preflight` rulează ca
`ems-device`, verifică configurația și SQLite prin `mode=ro`, fără să creeze
identitate, să migreze schema ori să deschidă RS485. `health`, `identity` și
`dead-letter` deschid și ele baza read-only și pot rula în timpul monitorizării.

Installerul oprește serviciul înainte de `provision`, inclusiv dacă systemd
pregătește un auto-restart. Recovery-ul este înregistrat înainte de stop.
După activare, serviciul trebuie să rămână activ la cinci verificări la o
secundă distanță. La eșec, `current` revine la release-ul anterior, iar serviciul
este repornit. `previous` păstrează ținta anterioară. Configurația, identitatea,
credentialele și outbox-ul rămân în directoarele persistente.

Updaterul operatorului verifică minisign înainte de a folosi manifestul și
SHA-256 înainte de extracție. HTTPS nu urmează redirect-uri și ignoră proxy-urile
din mediu. Limite: manifest/semnătură 64 KiB fiecare, arhivă 64 MiB, extracție
256 MiB și 4.096 intrări; minimum 512 MiB liberi pe filesystem-ul release-urilor.
Arhivele cu traversal, link-uri, fișiere speciale, setuid/setgid, virtualenv
preconstruit sau bază SQLite a device-ului sunt refuzate.

### Recovery local

Dacă un update manual eșuează, inspectează `journalctl -u ems-device` și
`readlink -f /opt/ems-device/current`. Nu șterge `agent.lock`: unlink-ul unui
lock activ permite unui alt proces să creeze un inode nou și să pornească un
al doilea master. Un proces pornit manual trebuie oprit de operator înainte de
provisioning/reset; installerul controlează serviciul systemd, nu procese arbitrare.

Pentru revenire explicită la `previous`, după verificarea release-ului dorit:

```sh
sudo sh -c '
set -eu
exec 9>/opt/ems-device/.update.lock
flock -n 9
previous=$(readlink -f /opt/ems-device/previous)
test -x "$previous/.venv/bin/ems-device"
ln -sfn "$previous" /opt/ems-device/.current.new
mv -Tf /opt/ems-device/.current.new /opt/ems-device/current
systemctl restart ems-device
systemctl is-active --quiet ems-device
'
```

Release-urile nu sunt șterse automat. Păstrează întotdeauna țintele `current`
și `previous`; eliberează doar release-uri vechi după verificarea manuală a
monitorizării. Un release semnat deja prezent este refuzat, nu suprascris.

### Stadiu P0 #13 / #3 și limite de validare

Versiunea aplicației EMS are o singură sursă (`pyproject.toml`, PEP 440) și este
citită din metadata pachetului instalat. Enrollment-ul pending și heartbeat-ul
linked trimit aceeași versiune, build Git (când disponibil), hardware platform,
arhitectură și versiune OS. Acestea nu sunt versiunea firmware-ului DEYE.
Valorile necunoscute sunt omise. La un checkout modificat build-ul are sufixul
`-dirty`; o arhivă fără build metadata nu primește un commit inventat.

Testele locale rulează shell-ul real al installerului cu comenzi OS/systemd
izolate și un proces real care ține `flock`; verifică eliberarea lock-ului,
rollback-ul și păstrarea identității/configurației. Un test de updater creează
un virtualenv real și execută scriptul după activare. Alte teste simulează
HTTP/signing/systemd, disk-full și întreruperi ale tranzacțiilor SQLite.
Reset-ul soft mută vechea coadă în dead-letter (`assignment_reset`) și șterge
cache-ul stației în aceeași tranzacție cu credentialele; nu transmite datele
vechii stații către o nouă asociere.

Acesta rămâne un updater inițiat de operator. #13 rămâne deschis pentru
contractul fleet OTA (oferte/confirmare din noul proces), helper/watchdog
independent, replay/downgrade policy, artifact offline cu dependențe complete și
recuperare automată după power-cut/SIGKILL. Cinci secunde de `is-active` nu sunt
o confirmare de health sau de conectivitate la platformă. #3 mai are validarea
lifecycle cu platforma reală și recuperarea rotației pierdute. Nu s-au rulat
systemd real, power-cut pe SD, SSH pe Pi sau invertor fizic; #1/#2 rămân
condiționate de profilul și validarea hardware. Scrierile în invertor rămân
dezactivate.

## Buget de resurse (măsurat, dar NU pe Raspberry Pi)

Măsurătorile de mai jos vin dintr-un container de dezvoltare x86-64, NU de
pe hardware ARM64 real -- numerele reale pe Raspberry Pi 4 pot diferi
(arhitectură diferită, encoding Python diferit posibil, I/O SD mai lent).
Tratează-le ca ordin de mărime, nu ca specificație:

| Resursă | Măsurat (x86-64, dev container) | Note |
|---|---|---|
| Disc, venv producție (fără `[test]`) | ~36 MB | `httpx`+`anyio`+`certifi`+`h11`+`httpcore`+`idna`+`typing_extensions`, `pymodbus`+`pyserial` |
| Disc, cod sursă (`src/`) | ~160 KB | |
| Disc, stare SQLite goală (WAL+db) | ~60 KB | crește cu backlog-ul (max 17.280 mostre; vezi README pentru estimarea la capacitate) |
| RSS, invocare CLI reală (`health`) | ~25 MB | proces scurt, se termină; NU rulează continuu |
| RSS, doar import module | ~24 MB | |

**Neconfirmat pe hardware real, deci explicit în afara acestui PR:** consum
CPU susținut pe perioade lungi (loop 10s cu polling Modbus real), uzura SD
reală sub scrierile WAL, comportament sub throttling termic Pi 4, timp de
boot la instalare completă (`apt-get`+`useradd`+venv), comportament sub
adaptor USB-RS485 real deconectat/reconectat în timpul rulării.

## Ce rămâne doar pe hardware real (nu s-a pretins altfel)

Criteriile de acceptare ale issue #4 cer explicit "teste power-loss/SQLite
recovery, storage full... deconectare USB" și "matrice reală Pi 4
4GB/OS/adaptor/invertor". Ce s-a putut face fără hardware, în acest PR:

- **Storage full**: `tests/test_agent.py::test_disk_full_during_sample_does_not_corrupt_queue_or_crash`
  și `test_disk_full_while_recording_health_error_does_not_mask_original_error`
  simulează `sqlite3.OperationalError('database or disk is full')` pe scrierea
  din `enqueue`/`record_error` -- confirmă contractul SOFTWARE (nu corupe
  coada, nu crapă procesul, se recuperează curat cand spațiul revine).
  Nu au fost necesare modificări de cod: `Agent.sample()`/`_record_error`
  gestionau deja acest caz prin `try/except` existent.
- **Power-loss/SQLite recovery real**: NU e testabil portabil -- validarea
  reală cere întreruperea alimentării fizic în timpul unei scrieri pe un
  card SD real, ceva ce niciun test unitar/CI nu poate simula onest (a
  "simula" ar însemna doar a testa ipoteze despre WAL, nu comportamentul
  real al mediului de stocare). WAL + `synchronous=FULL` (deja în `state.py`)
  reduc riscul teoretic; rămâne neconfirmat empiric.
- **Deconectare USB reală**: `test_modbus_sample_failure_increments_rs485_health`
  (existent) acoperă eșecul `client.connect()`, dar nu comportamentul unui
  adaptor USB-RS485 fizic scos/repus în timpul unei tranzacții Modbus reale.
- **Matrice Pi 4/OS/adaptor/invertor, integrare cu platforma FastAPI+PostgreSQL
  reală**: nu s-au putut rula în acest mediu de dezvoltare (fără hardware
  Raspberry Pi conectat). Rămâne explicit neacoperit de acest PR, urmărit
  separat.
