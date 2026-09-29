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

### Confirmare și watchdog independent (agent 0.1.2+)

`run.sh` instalează helper-ul root-owned, exclusiv stdlib, în
`/usr/local/lib/ems-device/update_watchdog.py`, separat de `current` și de
virtualenv-ul aplicației. Timer-ul `ems-device-update-watchdog.timer` rulează
la 15 secunde și după boot; nu descarcă și nu instalează release-uri automat.
Updaterul semnat refuză activarea dacă timer-ul nu este activ.

Înainte de switch se scrie și se sincronizează pe disc un jurnal cu attempt ID,
release țintă/anterior, versiune, boot OS și deadline monotonic. Jurnalul și
symlink-urile folosesc replace atomic plus fsync pe director. După switch,
`ems-device-update` raportează activare în așteptarea confirmării, nu succes.
Agentul publică un fișier local `runtime-health.json` (0600) numai după contact
reușit cu platforma: pending enrollment sau config+heartbeat pentru assigned.
Release-ul procesului este capturat la import, înainte ca `current` să poată fi
schimbat de un updater concurent.

Watchdog-ul verifică attempt ID, release, versiune, boot_id nou, boot OS,
timestamp monotonic proaspăt și PID-ul MainPID al serviciului activ. Fișierele
health sunt limitate la 64 KiB și trebuie să fie fișiere normale, fără symlink.
Lipsa confirmării în 120 secunde, reboot-ul înainte de confirmare sau activarea
întreruptă restaurează release-ul anterior. O întrerupere în timpul rollback-ului
lasă intenția în jurnal și următorul tick o reia. Eșecul restartului anterior
rămâne vizibil ca `rollback_failed`, cu motiv sanitizat; este necesară recuperare
manuală. Noul update/installer este refuzat cât timp un update cere recovery.
După repararea release-ului anterior, operatorul poate reîncerca explicit:
`sudo /usr/bin/python3 -I /usr/local/lib/ems-device/update_watchdog.py --retry-rollback`.
Update-ul semnat cere o instalare bootstrap existentă; prima instalare folosește
`run.sh`, astfel încât să existe întotdeauna o țintă de rollback.

`ems-device ... health` afișează `update.current`, `previous`, `phase`,
`attempt_id`, `version` și `last_error`. Diagnostic root suplimentar:

```sh
sudo systemctl status ems-device-update-watchdog.timer
sudo journalctl -u ems-device-update-watchdog.service
```

Timer-ul validează local contactul noului proces cu platforma; nu implementează
încă raportarea/confirmarea deployment-ului fleet. Contractul platformei #168
semnează artifactul, dar oferta curentă nu include manifestul semnat cu
compatibilitate/build/protocol și nu oferă retry idempotent pentru evenimentele
intermediare ori evenimente autentificate de la device-uri pending. Aceste
extensii trebuie coordonate înainte de automatizarea fleet; nu sunt simulate
prin endpoint-uri inventate.

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
contractul fleet OTA (oferte și evenimente/confirmare către platformă), politici
replay/downgrade, artifact offline și validarea pe Pi. Testele de proces folosesc
`os._exit` înainte/după switch și în timpul rollback-ului; verifică recuperarea
jurnalului, nu comportamentul electric al unui SD la power-cut. Watchdog-ul este
testat ca script independent de mediul aplicației, cu systemd izolat în teste.
#3 mai are validarea lifecycle cu platforma reală și recuperarea rotației pierdute.
Nu s-au rulat systemd real pe Pi, power-cut pe SD, SSH pe Pi sau invertor fizic;
#1/#2 rămân condiționate de profilul și validarea hardware. Scrierile în invertor
rămân dezactivate.

Reader-ul respinge blocuri care includ registre nedeclarate și păstrează
separat adresele FC03/FC04. Dacă un profil auditat declară `unavailable_values`,
punctele indisponibile și sumele care depind de ele lipsesc din mostră și apar
în `quality_flags.unavailable_fields`; zero măsurat rămâne zero. O citire complet
indisponibilă produce eroare de eșantionare, fără a încărca o mostră inventată.
Opțional, `identity_checks` verifică registre exacte de model/firmware înainte
de telemetrie și după reconectare. Eșecul produce `incompatible_identity_registers`;
verifică profilul și unitatea conectată înainte de a modifica valorile așteptate.
Profilul candidat livrat nu conține valori de identitate/sentinele presupuse.
Aceste mecanisme sunt testate prin transport simulat și SQLite real, fără
validare electrică pe magistrala unui invertor.

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

## Diagnostic clone / placă înlocuită

`HardwareIdentityError` în jurnal oprește folosirea credentialelor pe o placă
diferită ori când serialul anterior nu mai poate fi citit. `ems-device ... identity`
și `health` funcționează fără re-enrollment și arată fingerprint-urile publice.
Pe aceeași placă, restaurează accesul la Device Tree; pentru înlocuirea intenționată
a plăcii, folosește factory reset cu confirmarea exactă a serialului, apoi noul
Device Code. Nu copia binding-ul ori secretele de pe alt device ca remediu.
Testele folosesc fișiere Device Tree sintetice, o copie reală a directorului
SQLite și HTTP mock; nu constituie attestation sau verificare pe Pi fizic.
