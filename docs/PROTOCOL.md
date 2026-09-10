# Contract v0.1 și extensii

## Compatibilitate curentă

Contract inspectat în `bbogdan59/EMS-management-platform`, baza inițială `d1426b9`. API-ul folosit este cel existent, nu endpoint-uri propuse:

| Operație | Endpoint | Comportament agent |
|---|---|---|
| Asociere | POST /api/v1/devices/claim | cod temporar + device_name + hardware_info; salvează device_id/station_id/credential_secret |
| Configurație | GET /api/v1/config | cache config_version/preference_version; nu confirmă aplicarea în invertor |
| Prezență | POST /api/v1/devices/heartbeat | boot_id, firmware_version agent, telemetry=true, inverter_write=false |
| Telemetrie | POST /api/v1/telemetry/batch | items; elimină batch numai când accepted+duplicates acoperă toate elementele și rejected=0 |

Autentificare `Authorization: Bearer <device_id>.<credential_secret>` prin HTTPS verificat. Redirect-urile nu sunt urmate. UUID-ul local este inventar; nu secret și nu mecanism pentru alegerea tenant-ului. Raspberry Pi nu necesită IMEI; eventualul IMEI al unui modem este tot metadată de inventar.

## Mostre

`boot_id` nou per proces; `sequence` monoton per boot. Mostrele persistate păstrează ambele valori la retry. `measured_at` este UTC cu timezone. `schema_version=1`.

| Câmp | Unitate și convenție |
|---|---|
| pv_power_w | W, producție instantanee >=0 |
| load_power_w | W, consum >=0 |
| grid_power_w | W, pozitiv import, negativ export |
| battery_power_w | W, pozitiv încărcare, negativ descărcare |
| battery_soc_percent | 0..100 % |

`quality_flags.simulated` identifică simulatorul. Datele demo sunt permise numai explicit și pentru stații demo. Câmpurile neobservate rămân absente; EV nu este dedus din puterea casei. Din puteri se pot deriva import/export și charge/discharge separate, dar energia kWh cere integrare temporală sau contoare cumulative, încă neimplementate aici.

## Profil RS485 local

PyModbus 3.11.3, [API oficial](https://pymodbus.readthedocs.io/en/v3.11.3/source/client.html). Un singur apel serial la un moment dat. Profil JSON cu `verified`, model exact, firmware validat, sursă/revizie protocol și `points`. Fiecare punct are:

- `field`: unul dintre cele cinci câmpuri de mai sus, fără duplicate;
- `address`: adresă PDU zero-based confirmată, 0..65535;
- `function`: numai 3 holding / 4 input;
- `encoding`: numai u16/s16; `scale`: multiplicator către W sau %, inclusiv inversarea semnului dacă protocolul o cere.

Nu există adrese demonstrative care ar putea fi confundate cu registre DEYE reale. Profilul minim are 1..32 puncte. 32-bit, word-order, sentinele de indisponibilitate, identitate invertor, gruparea blocurilor și detectarea modelului sunt în backlog. Un profil local greșit poate produce valori plauzibile: validarea hardware rămâne obligatorie.

## Flux propus ulterior (nu este implementat)

1. Device nou face enrollment autentificat cu cheie proprie/provisioning secret; primește starea pending, fără tenant ales de client.
2. Operatorul autorizat îl alocă unei stații/tenant, cu audit și protecție împotriva însușirii prin serie ghicită.
3. Serverul livrează configurația de conexiune și configurația dorită versionată.
4. Device identifică modelul/firmware-ul, citește numai registrele cunoscute și raportează snapshot-ul observat cu proveniență și hash.
5. Schimbările web devin comenzi limitate în timp și idempotente, cu allowlist pe model. Device verifică limitele locale, scrie și face readback; raportează applied/rejected/failed.

Nu promite controlul tuturor parametrilor sau aplicare instantanee. Registrele de protecție a rețelei și parametrii instalatorului necesită o politică distinctă. La pierderea cloud-ului, acest subset nu modifică regimul invertorului.
