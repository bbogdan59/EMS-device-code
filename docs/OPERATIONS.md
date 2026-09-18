# Operare Pi: actualizări, resurse, ce rămâne doar pe hardware real (issue #4)

## Actualizări verificate cu rollback

Mecanismul canonic este `ems-device-update`, documentat în README. Manifestul
este verificat cu cheia publică minisign fixată de operator, iar arhiva este
verificată SHA-256 înainte de extracție. Fiecare release primește propriul
virtualenv sub `/opt/ems-device/releases/<version>`; după testul local `health`,
symlink-ul `/opt/ems-device/current` este schimbat atomic. Dacă serviciul nu
devine activ după restart, updaterul reactivează release-ul anterior și îl
repornește.

`tests/test_updater.py` acoperă verificarea/activarea, rollback-ul la eșecul
systemd și respingerea traversal-ului din arhivă. Testele folosesc mock-uri și
nu înlocuiesc o întrerupere fizică de alimentare pe Pi. `git pull && sudo
./run.sh` rămâne numai fluxul manual de bootstrap/dezvoltare, nu canalul de
update pentru producție.

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
