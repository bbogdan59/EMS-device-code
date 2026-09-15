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

**Nu este inclusă o hartă DEYE validată.** Modelul și firmware-ul nu au fost specificate. Exemplul din `profiles/` este intenționat nevalidat și nu poate porni citirea. Nu copia registre de la altă familie DEYE. Marcarea `verified=true` este o atestare a operatorului, nu autodetecție sau certificare realizată de software.

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
firmware-ul DEYE exact. Pentru update: `git pull && sudo ./run.sh`; fișierul de
config și identitatea existentă sunt păstrate.

Nu clona `/var/lib/ems-device`: conține secretul unic al unității. O imagine OS
de producție trebuie să lase acel director gol, astfel încât fiecare unitate să
primească altă identitate la provisioning.

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

## Dezvoltare

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/pytest -q
```

CI verifică Python 3.11 și 3.13. Testele MockTransport și Modbus fake nu reprezintă validare pe Pi/invertor sau integrare cu un server web real.

## Limite și contracte

Vezi [docs/PROTOCOL.md](docs/PROTOCOL.md) pentru API, semne, coadă, profil și extensiile necesare. Frecvența implicită este 10 secunde, nu timp real garantat. Coada păstrează maximum 17.280 mostre (aproximativ 48 ore la 10 secunde); când este plină, refuză mostre noi, incrementează contorul health și păstrează mostrele vechi. SQLite rulează WAL + `synchronous=FULL`; asta reduce riscul la întreruperea procesului/alimentării, dar nu înlocuiește teste reale cu SD și power-cut. Contractul web per-item permite acum retry selectiv/dead-letter; dead-letter-ul nu are încă retenție automată și trebuie monitorizat prin `health`. Sincronizarea ceasului prin OS/NTP este necesară.

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
