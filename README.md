# EMS device agent

Primul subset funcțional al controllerului local Python pentru EMS. **v0.1 este exclusiv read-only**: nu execută planuri/comenzi și nu scrie registre, chiar dacă platforma indică `execution_mode=live`.

## Implementat

- UUID persistent al instalației, distinct de `device_id` atribuit de server și seria invertorului.
- Asociere prin cod temporar creat în platformă pentru o stație; credentiale locale cu permisiuni restrictive și legate de originea HTTPS.
- Citire periodică `/api/v1/config`, cache local și heartbeat cu capabilități reale.
- Telemetrie PV, consum, grid, putere baterie și SOC; valori necunoscute omise, fără zero inventat.
- Outbox SQLite persistent, batch-uri de 50, retry cu backoff, identificatori stabili pentru deduplicare după timeout/restart.
- Simulator explicit și transport Modbus RTU RS485 read-only bazat pe profil local auditat.
- Serviciu systemd, oprire SIGTERM/SIGINT și teste fără hardware.

**Nu este inclusă o hartă DEYE validată.** Modelul și firmware-ul nu au fost specificate. Exemplul din `profiles/` este intenționat nevalidat și nu poate porni citirea. Nu copia registre de la altă familie DEYE. Marcarea `verified=true` este o atestare a operatorului, nu autodetecție sau certificare realizată de software.

## OS și hardware

Recomandare pentru Raspberry Pi 4 4GB: **Raspberry Pi OS Lite 64-bit**, fără desktop. [Pagina oficială](https://www.raspberrypi.com/software/operating-systems/) confirmă compatibilitatea 4B. Folosește Python 3.11+ într-un virtualenv și un adaptor USB–RS485 izolat, cu cale stabilă `/dev/serial/by-id/`. Codul este portabil pe Linux ARM64/x86-64; alte plăci necesită validarea distribuției și adaptorului.

Pinout-ul, portul corect, baud/parity, terminarea și posibilitatea de a partaja magistrala trebuie verificate în documentația modelului exact. Un singur master pe segmentul RS485. Nu confunda conectorul BMS/CAN cu portul Modbus. Agentul nu scanează adrese sau baudrate-uri automat.

## Instalare pe Pi

Din checkout-ul repository-ului:

```sh
sudo apt update
sudo apt install python3-venv git
sudo useradd --system --user-group --home-dir /var/lib/ems-device --create-home ems-device
sudo usermod -aG dialout ems-device
sudo mkdir -p /opt/ems-device /etc/ems-device
sudo cp -r src pyproject.toml /opt/ems-device/
sudo python3 -m venv /opt/ems-device/.venv
sudo /opt/ems-device/.venv/bin/pip install /opt/ems-device
sudo install -m 640 -o root -g ems-device config.example.toml /etc/ems-device/config.toml
```

Editează URL-ul, reader-ul și portul/profilul dacă ai hardware validat. Exemplul refuză implicit upload-ul simulatorului. Pentru demo setează `allow_simulated_upload=true` și folosește **o stație demo dedicată în shadow**; platforma actuală nu garantează excluderea fixture-urilor din optimizare.

În platforma web creează stația și generează codul de asociere. Agentul nu alege tenant-ul prin seria declarată:

```sh
sudo -u ems-device /opt/ems-device/.venv/bin/ems-device --config /etc/ems-device/config.toml identity
sudo -u ems-device /opt/ems-device/.venv/bin/ems-device --config /etc/ems-device/config.toml enroll
sudo install -m 644 deploy/ems-device.service /etc/systemd/system/ems-device.service
sudo systemctl daemon-reload
sudo systemctl enable --now ems-device
journalctl -u ems-device -f
```

Codul este cerut prin prompt ascuns, nu argument shell. O cerere claim cu rezultat necunoscut nu este repetată automat: serverul actual consumă codul și returnează secretul o singură dată. Operatorul trebuie să reconcilieze/revoce asocierea și să reprovisioneze cu un cod nou. Nu șterge starea ca metodă obișnuită de retry. Nu clona directorul `/var/lib/ems-device` pe alte dispozitive; conține identitatea și credentialele. Reinstalarea fără acest director creează altă identitate.

`--once` execută un ciclu de diagnostic, nu certifică sănătatea sistemului; inspectează logurile. Nu deschide porturi inbound; conexiunile spre web sunt outbound HTTPS.

## Dezvoltare

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/pytest -q
```

CI verifică Python 3.11 și 3.13. Testele MockTransport și Modbus fake nu reprezintă validare pe Pi/invertor sau integrare cu un server web real.

## Limite și contracte

Vezi [docs/PROTOCOL.md](docs/PROTOCOL.md) pentru API, semne, coadă, profil și extensiile necesare. Frecvența implicită este 10 secunde, nu timp real garantat. Coada păstrează maximum 17.280 mostre (aproximativ 48 ore la 10 secunde); când este plină, refuză mostre noi și raportează eroare, păstrând mostrele vechi. La respingere parțială server-side întregul batch rămâne în coadă: rezolvarea per-item/dead-letter este un pas ulterior. Sincronizarea ceasului prin OS/NTP este necesară.

Implementările viitoare sunt urmărite prin GitHub issues în acest repo și în EMS-management-platform: enrollment automat nealocat, configurație desired/reported, profil DEYE verificat, contoare/diagnoză și scrieri controlate cu readback.

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
