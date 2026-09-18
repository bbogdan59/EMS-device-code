# EMS device agent

Primul subset funcțional al controllerului local Python pentru EMS. **v0.1 este exclusiv read-only**: nu execută planuri/comenzi și nu scrie registre, chiar dacă platforma indică `execution_mode=live`.

## Implementat

- Identitate unică generată după instalarea OS: UUID intern, serial public, cod de activare pentru client și secret de provisioning local.
- Enrollment automat idempotent, stare pending și polling până când clientul asociază device-ul; nu mai este necesar codul temporar de 15 minute în CLI.
- Citire periodică `/api/v1/config`, cache local și heartbeat cu capabilități reale.
- Telemetrie PV, consum, grid, putere baterie și SOC; valori necunoscute omise, fără zero inventat.
- Outbox SQLite persistent, batch-uri de 50, retry cu backoff, identificatori stabili pentru deduplicare după timeout/restart.
- Simulator explicit și transport Modbus RTU RS485 read-only bazat pe profil local auditat.
- Serviciu systemd, oprire SIGTERM/SIGINT și teste fără hardware.

**Nu este inclusă o hartă DEYE validată pe hardware real.** `profiles/schema-example.json` rămâne intenționat gol/nevalidat. `profiles/deye_sg04lp3_candidate.json` (issue #1) adaugă un candidat mult mai complet pentru familia Deye SUN-*K-SG04LP3-EU (5/6/8/10/12K, inclusiv varianta 10K) -- adrese/encodări/scale transcrise dintr-o sursă comunitară citată explicit (nu documentația oficială, blocată de rețea în acest mediu), dar **livrat tot cu `verified: false`**: nimeni nu l-a citit de pe un invertor real în această sesiune. Vezi `docs/VALIDATION_SG04LP3.md` pentru exact ce rămâne de confirmat (în special sensul puterii/curentului de baterie) înainte ca un operator să îl comute pe `verified: true`. Nu copia registre de la altă familie DEYE. Marcarea `verified=true` este o atestare a operatorului, nu autodetecție sau certificare realizată de software.

## OS și hardware

Recomandare pentru Raspberry Pi 4 4GB: **Raspberry Pi OS Lite 64-bit**, fără desktop. [Pagina oficială](https://www.raspberrypi.com/software/operating-systems/) confirmă compatibilitatea 4B. Folosește Python 3.11+ într-un virtualenv și un adaptor USB–RS485 izolat, cu cale stabilă `/dev/serial/by-id/`. Codul este portabil pe Linux ARM64/x86-64; alte plăci necesită validarea distribuției și adaptorului.

Pinout-ul, portul corect, baud/parity, terminarea și posibilitatea de a partaja magistrala trebuie verificate în documentația modelului exact. Un singur master pe segmentul RS485. Nu confunda conectorul BMS/CAN cu portul Modbus. Agentul nu scanează adrese sau baudrate-uri automat.

## Cele două etape de instalare

### 1. Tehnician: pregătirea unității

Recomandarea este Raspberry Pi OS Lite 64-bit. După primul boot al unei imagini
curate, tehnicianul rulează:

```sh
git clone https://github.com/bbogdan59/EMS-device-code.git
cd EMS-device-code
sudo EMS_PLATFORM_URL=https://ems.example.com ./run.sh
```

`run.sh` este repetabil: instalează dependențele, utilizatorul izolat, virtualenv-ul,
configurația inițială și serviciul systemd. Identitatea este creată în
`/var/lib/ems-device`, nu în checkout sau în imaginea OS. La final afișează:

- seria publică, pentru suport/inventar;
- un `Device code` cu entropie mare, care trebuie tipărit în interiorul sau pe
  sigiliul pachetului și nu publicat în poze/listări;
- starea enrollment-ului.

Configurația sigură implicită este `reader="disabled"`: device-ul se poate
înregistra și trimite heartbeat, dar nu pretinde că citește invertorul. Trecerea
la `modbus` se face numai după instalarea unui profil validat pentru modelul și
firmware-ul DEYE exact. `git pull && sudo ./run.sh` este doar fluxul manual de
bootstrap/dezvoltare; fișierul de config și identitatea existentă sunt păstrate.

### Update verificat și rollback

Producția trebuie să publice un `manifest.json` cu exact câmpurile `version`,
`url` (arhivă `.tar.gz` HTTPS) și `sha256`, plus semnătura detached
`manifest.json.minisig`. Cheia privată nu ajunge pe device. Operatorul fixează
cheia publică minisign dintr-un canal separat și rulează, ca root:

```sh
/opt/ems-device/current/.venv/bin/ems-device-update \
  --public-key 'RW...' https://updates.example.com/stable/manifest.json
```

Updaterul refuză HTTP, redirect-uri, manifesturi cu alte câmpuri, hash-uri
greșite și arhive cu traversal/link-uri. Instalează într-un director nou,
construiește un virtualenv izolat, execută `health`, apoi schimbă atomic symlink-ul
`/opt/ems-device/current`. Dacă serviciul nu devine activ, restaurează release-ul
anterior și îl repornește. Descărcarea periodică nu este activată implicit:
fereastra de mentenanță și politica de rollout rămân decizia operatorului.

Nu clona `/var/lib/ems-device`: conține secretul unic al unității. O imagine OS
de producție trebuie să lase acel director gol, astfel încât fiecare unitate să
primească altă identitate la provisioning.

**Alternativ, de pe laptopul tehnicianului, fără SSH manual pe Pi**: vezi
[configurator/README.md](configurator/README.md) -- un script care se conectează
prin SSH la Pi (IP autodetectat sau introdus manual, user/parolă), copiază acest
checkout și rulează `sudo ./run.sh` acolo, relantand seria/Device Code-ul direct
in terminalul local. Nu reimplementeaza nimic din `run.sh`; e doar un wrapper
peste exact fluxul manual de mai sus.

### 2. Client: instalarea acasă

Clientul conectează alimentarea/rețeaua și adaptorul RS485, apoi introduce
`Device code` de pe eticheta sigilată în stația sa din platforma web. Agentul
face numai conexiuni HTTPS outbound și verifică periodic assignment-ul. După
asociere primește credentiala device-ului, o stochează local și începe să ia
configurația stației. Clientul nu editează fișiere, nu intră prin SSH și nu
sincronizează o fereastră de 15 minute cu tehnicianul.

Diagnostic sigur, fără afișarea secretelor:

```sh
sudo -u ems-device /opt/ems-device/.venv/bin/ems-device --config /etc/ems-device/config.toml identity
sudo -u ems-device /opt/ems-device/.venv/bin/ems-device --config /etc/ems-device/config.toml health
sudo -u ems-device /opt/ems-device/.venv/bin/ems-device --config /etc/ems-device/config.toml dead-letter
journalctl -u ems-device -f
systemctl status ems-device
```

`--once` execută un ciclu de diagnostic, nu certifică sănătatea sistemului; inspectează logurile. Nu deschide porturi inbound; conexiunile spre web sunt outbound HTTPS.

Comanda `health` produce JSON fără secrete cu versiunea agentului, integritatea
SQLite, utilizarea și vechimea cozii, mostre refuzate, ultimele momente de
citire/upload/config și contoare de erori (inclusiv RS485). Scrierile timestamp
de succes sunt limitate la una pe minut pentru a reduce uzura SD. Starea
`clock_sync=unknown` înseamnă că markerul systemd-timesyncd nu este disponibil,
nu dovedește că ceasul este greșit; imaginile care folosesc alt daemon NTP
trebuie să adauge o verificare specifică și testată.

`dead-letter` listează local doar `boot_id`, `sequence`, motivul și momentul
respingerilor permanente, fără payload-ul brut. După introducerea contractului
web per-item, mostrele acceptate/duplicate sunt eliminate, cele retryable rămân
în outbox, iar cele permanente sunt mutate atomic în dead-letter. Un răspuns
incomplet, cu identitate greșită sau cu totaluri contradictorii nu modifică
deloc coada. Agentul rămâne compatibil cu serverele vechi: fără `results`,
șterge batch-ul numai la succes agregat integral.

### Transfer, revocare, factory reset (issue #3)

Nicio revocare/transfer/factory-reset declanșat din admin-ul web nu ajunge
direct la device -- singurul semnal e un `401`/`403` la următorul apel
autentificat (`journalctl` arată tipul `CredentialInactiveError`, niciodată
textul răspunsului). Recuperarea e mereu manuală, cerută explicit de
operator, niciodată automată:

```sh
# elibereaza asocierea curenta (transfer catre alta statie/platforma);
# identitatea fizica (serial/UUID) ramane neschimbata, un Device Code nou e emis
sudo -u ems-device /opt/ems-device/.venv/bin/ems-device --config /etc/ems-device/config.toml \
  reset --confirm-serial EMS-XXXX-XXXX-XXXX

# reprovizionare completa (hardware repus in circuit pentru alt client):
# emite o identitate noua in intregime, sterge coada/dead-letter locale
sudo -u ems-device /opt/ems-device/.venv/bin/ems-device --config /etc/ems-device/config.toml \
  reset --factory --confirm-serial EMS-XXXX-XXXX-XXXX
```

`--confirm-serial` trebuie să fie EXACT serialul afișat de `identity` -- fără
potrivire exactă, comanda refuză și nu schimbă nimic. După `reset`, urmatorul
`provision`/`run` reia enrollment-ul automat (eventual către un `platform_url`
nou din `config.toml`, acum că `platform_origin` local a fost eliberat).

`rotate-credential` cere platformei un secret nou pentru identitatea deja
alocată (`POST /devices/credentials/rotate`, autentificat cu secretul curent).
Marchează local o rotație "in curs" ÎNAINTE de cererea de rețea, ca un răspuns
pierdut (crash, retea cazuta) să rămână vizibil (`health` →
`credential_rotation_pending: true`) în loc să dispară tăcut -- agentul nu
poate distinge singur "rotația a reușit dar am pierdut răspunsul" de "a fost
efectiv revocat", așa că nu reîncearcă orbește; recuperarea folosește tot
`reset`.

## Dezvoltare

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/pytest -q
```

CI verifică Python 3.11 și 3.13. Testele MockTransport, Modbus fake și updater
mock nu reprezintă validare pe Pi/invertor, power-cut sau integrare cu un server
web real. Rollback-ul testat local acoperă eșecul verificării systemd, nu o
întrerupere fizică în timpul schimbării release-ului. Bugetele măsurate și
limitele validării sunt documentate în [docs/OPERATIONS.md](docs/OPERATIONS.md).

## Limite și contracte

Vezi [docs/PROTOCOL.md](docs/PROTOCOL.md) pentru API, semne, coadă, profil și extensiile necesare, și [docs/OPERATIONS.md](docs/OPERATIONS.md) pentru actualizări verificate cu rollback, buget de resurse măsurat (dar nu pe Pi) și ce rămâne explicit doar pe hardware real. Frecvența implicită este 10 secunde, nu timp real garantat. Coada păstrează maximum 17.280 mostre (aproximativ 48 ore la 10 secunde); când este plină, refuză mostre noi, incrementează contorul health și păstrează mostrele vechi. SQLite rulează WAL + `synchronous=FULL`; asta reduce riscul la întreruperea procesului/alimentării, dar nu înlocuiește teste reale cu SD și power-cut. Contractul web per-item permite acum retry selectiv/dead-letter; dead-letter-ul nu are încă retenție automată și trebuie monitorizat prin `health`. Sincronizarea ceasului prin OS/NTP este necesară.

Implementările viitoare sunt urmărite prin GitHub issues în acest repo și în EMS-management-platform: self-service claim în web, transfer/factory reset, configurație desired/reported, profil DEYE verificat, contoare/diagnoză și scrieri controlate cu readback.

## Următorii pași, în paralel

| Repository | Issue | Lucrare |
|---|---|---|
| Device | [#1](https://github.com/bbogdan59/EMS-device-code/issues/1) | Profil DEYE verificat și citiri extinse |
| Device | [#2](https://github.com/bbogdan59/EMS-device-code/issues/2) | Executor cu limite locale și readback |
| Device | [#3](https://github.com/bbogdan59/EMS-device-code/issues/3) | Enrollment automat și lifecycle |
| Device | [#4](https://github.com/bbogdan59/EMS-device-code/issues/4) | Operare Pi, coadă, health și E2E |
| Web | [#16](https://github.com/bbogdan59/EMS-management-platform/issues/16) | Enrollment pending și alocare tenant |
| Web | [#17](https://github.com/bbogdan59/EMS-management-platform/issues/17) | Configurație desired/reported și snapshot |
| Web | [#18](https://github.com/bbogdan59/EMS-management-platform/issues/18) | Telemetrie extinsă și ACK per item |

Mai întâi: device #1 în paralel cu web #16–18 (contracte comune stabilite înainte). Device #3 depinde de web #16; executorul #2 depinde de profilul #1 și web #17. Fiecare issue conține criterii de acceptare și prompt pentru Claude.
