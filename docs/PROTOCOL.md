# Contract v0.1 și extensii

## Compatibilitate curentă

Contract inspectat în `bbogdan59/EMS-management-platform`, baza inițială `d1426b9`. API-ul folosit este cel existent, nu endpoint-uri propuse:

| Operație | Endpoint | Comportament agent |
|---|---|---|
| Enrollment | POST /api/v1/devices/enroll | UUID + secret de provisioning + serial + cod de activare; pending până la claim-ul clientului |
| Configurație | GET /api/v1/config | cache config_version/preference_version; nu confirmă aplicarea în invertor |
| Prezență | POST /api/v1/devices/heartbeat | boot_id, firmware_version agent, telemetry=true, inverter_write=false |
| Telemetrie | POST /api/v1/telemetry/batch | ACK per item: șterge accepted/duplicate, păstrează retryable, mută permanent în dead-letter; fallback sigur la răspunsul agregat vechi |

Înainte de assignment, dovada device-ului este `provisioning_secret`, generat și
păstrat local. După assignment, autentificarea este `Authorization: Bearer
<device_id>.<credential_secret>` prin HTTPS verificat. Redirect-urile nu sunt
urmate. Serialul public este inventar, nu secret și nu alege tenant-ul. Codul de
activare este un bearer secret separat, cu entropie mare, livrat sigilat
clientului și invalidat după primul claim reușit. Raspberry Pi nu necesită IMEI;
eventualul IMEI al unui modem este tot metadată de inventar.

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

Răspunsul nou al platformei adaugă `results` în aceeași ordine cu `items`.
Fiecare rezultat repetă `boot_id`/`sequence`, are status
`accepted|duplicate|rejected`, `retryable` și un `reason_code` stabil. Agentul
validează identitatea fiecărui rezultat și concordanța cu totalurile înainte
de orice mutație. Respingerea retryable rămâne în outbox; cea permanentă este
copiată și ștearsă atomic în SQLite, apoi poate fi inspectată local cu
`ems-device ... dead-letter`. Payload-ul brut nu este tipărit de comandă.

## Profil RS485 local

PyModbus 3.11.3, [API oficial](https://pymodbus.readthedocs.io/en/v3.11.3/source/client.html). Un singur apel serial la un moment dat. Profil JSON cu `verified`, model exact, firmware validat, sursă/revizie protocol și `points`. Fiecare punct are:

- `field`: unul dintre cele cinci câmpuri de mai sus, fără duplicate;
- `address`: adresă PDU zero-based confirmată, 0..65535;
- `function`: numai 3 holding / 4 input;
- `encoding`: numai u16/s16; `scale`: multiplicator către W sau %, inclusiv inversarea semnului dacă protocolul o cere.

Nu există adrese demonstrative care ar putea fi confundate cu registre DEYE reale. Profilul minim are 1..32 puncte. 32-bit, word-order, sentinele de indisponibilitate, identitate invertor, gruparea blocurilor și detectarea modelului sunt în backlog. Un profil local greșit poate produce valori plauzibile: validarea hardware rămâne obligatorie.

## Flux de instalare și stadiu

1. `run.sh` generează material unic după instalarea OS, îl păstrează cu mod
   `0600` în SQLite și încearcă enrollment-ul.
2. Agentul neasociat face polling cu backoff; nu descarcă config de tenant și
   nu pornește RS485 înainte de assignment.
3. Clientul introduce codul de activare sigilat în platformă. Serverul îl
   consumă atomic și leagă device-ul pending de stația autorizată.
4. Agentul recuperează idempotent credentiala bootstrap și face primul
   heartbeat autentificat. Codul de activare local este șters.
5. Serverul livrează configurația versionată. Modul `disabled` rămâne sigur
   până la disponibilitatea unui profil DEYE validat.

Pașii 1, 2, 4 și modul sigur sunt implementați în agent. Claim-ul self-service
din pasul 3 este contractul comun cu `EMS-management-platform#44`. Transferul,
revocarea/factory reset, identificarea DEYE și desired/reported complet rămân
work items separate.

Nu promite controlul tuturor parametrilor sau aplicare instantanee. Registrele de protecție a rețelei și parametrii instalatorului necesită o politică distinctă. La pierderea cloud-ului, acest subset nu modifică regimul invertorului.
