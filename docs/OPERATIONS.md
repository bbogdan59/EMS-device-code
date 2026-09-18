# Operare Pi: actualizări, resurse, ce rămâne doar pe hardware real (issue #4)

## Actualizări verificate cu rollback

Vezi comentariul din `deploy/update.sh` pentru mecanism. Pe scurt:
`run.sh`/`git pull && sudo ./run.sh` face backup la instalarea anterioară
înainte de a o înlocui; dacă noua versiune eșuează un test de fum minimal
(`import ems_device.cli`) sau serviciul systemd nu rămâne activ după
restart, backup-ul e restaurat automat și scriptul iese cu cod 3 -- unitatea
continuă să ruleze ultima versiune care chiar a funcționat, în loc să rămână
"brickuită" la mijlocul unei actualizări.

**"Verificat" înseamnă testat-cu-fum + rollback automat, NU o semnătură
criptografică.** Acest proiect nu are încă un proces stabilit de chei de
semnare/ancoră de încredere -- a pretinde unul ar însemna inventarea unei
infrastructuri pe care nimeni nu a cerut-o și nu o poate audita. Rămâne un
gol real, urmărit separat, nu ascuns tăcut.

`tests/test_update_rollback.sh` exercită mecanismul cu un venv real și
instalări pip reale (fără root/apt/systemd, deci rulează și în CI) pe 4
scenarii: instalare nouă bună, instalare nouă stricată (eșuează curat, fără
pretenție de rollback -- nu există ce restaura), upgrade bun-spre-bun,
upgrade bun-spre-stricat (rollback automat). Acest test a găsit și a permis
corectarea unui bug real în timpul dezvoltării: `pip install --upgrade`
dintr-un director local, cu numărul de versiune neschimbat, poate rămâne cu
un `build/lib/` cache neactualizat din instalarea anterioară -- deoarece
`cp -a`/`mv` păstrează timestamp-urile fișierelor, un fișier RESTAURAT (mai
vechi ca timestamp) poate părea "nu mai nou" pentru build-ul incremental
distutils, care atunci reutilizează cache-ul învechit în loc să recompileze.
Corectat prin `rm -rf build/` + `--force-reinstall --no-cache-dir` la
fiecare instalare/rollback.

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
