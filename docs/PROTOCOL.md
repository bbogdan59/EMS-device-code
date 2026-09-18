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

- `field`: unul dintre câmpurile din `ems_device.readers.FIELDS` (cele cinci originale plus extensiile issue #1: PV per string, rețea/load per fază, temperaturi, status brut, contoare cumulative), fără duplicate;
- `address`: adresă PDU zero-based confirmată, 0..65535 -- pentru puncte pe 32 de biți, adresa este primul din cele două registre adiacente;
- `function`: numai 3 holding / 4 input;
- `encoding`: u16/s16/u32/s32; punctele pe 32 de biți cer `word_order` (`low_high` -- registrul de la `address` e cuvântul jos, convenția obișnuită Deye pentru contoare -- sau `high_low`);
- `scale`: multiplicator către W/V/A/°C/% sau %, inclusiv inversarea semnului dacă protocolul o cere; `offset` (opțional, implicit 0) se adună DUPĂ scalare -- necesar pentru convenția Deye de temperatură (`raw*scale - 100`).

Opțional, profilul poate declara:

- `blocks`: listă de `{function, start, length}` -- registre citite într-un singur apel Modbus în loc de unul per punct. Fiecare bloc trebuie să acopere STRICT adrese deja documentate de puncte (nicio "traversare" a unei zone nedocumentate doar ca să unească două puncte apropiate); blocurile nu se pot suprapune; lungimea e limitată la `MAX_BLOCK_REGISTERS=60` (sub limita Modbus de 125, ca să reducă riscul unui cadru RS485 lung pe o magistrală/adaptor zgomotos). Fără `blocks`, comportamentul rămâne cel din v0.1 (un apel per punct).
- `computed`: câmpuri derivate ca sumă a altor puncte deja citite (ex. `pv_power_w` = `pv1_power_w` + `pv2_power_w`, pentru că Deye SG04LP3 nu expune un registru unic de putere PV totală). Fiecare termen din `sum_of` trebuie să fie un punct deja definit.
- `readiness_check`: `{field, allowed_values}` -- citit O SINGURĂ DATĂ, înainte de bucla periodică de eșantionare (`ModbusReader.check_ready()`, apelat din `cli.py` imediat după construirea reader-ului), ca să refuze devreme un profil incompatibil. Nu este o scanare de adrese/baudrate -- doar o citire a unui registru deja declarat ca punct, comparată cu valorile așteptate documentate.

Nu există adrese demonstrative care ar putea fi confundate cu registre DEYE reale. Profilul minim are 1..64 puncte. Sentinelele de indisponibilitate rămân în backlog (niciun profil livrat cu acest repo nu documentează încă unul confirmat -- vezi `docs/VALIDATION_SG04LP3.md`). Un profil local greșit poate produce valori plauzibile: validarea hardware rămâne obligatorie, iar `verified: true` este o atestare a operatorului dupa acea validare, niciodata a codului/agentului.

Primul profil candidat cu extensiile de mai sus, `profiles/deye_sg04lp3_candidate.json` (Deye SUN-*K-SG04LP3-EU, familia care include varianta 10K), este livrat cu `verified: false` -- sursa exactă și lista completă a ce rămâne de confirmat pe hardware real sunt în `docs/VALIDATION_SG04LP3.md`.

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

## Distribuirea release-urilor

Release-urile nu conțin `/var/lib/ems-device` și sunt instalate sub
`/opt/ems-device/releases/<version>`. Un manifest minisign verificat leagă
versiunea de URL-ul HTTPS și SHA-256-ul arhivei. Activarea schimbă atomic
`/opt/ems-device/current`; serviciul systemd folosește exclusiv acel symlink.
Un restart urmat de `systemctl is-active` nereușit reactivează release-ul
anterior. Cheia publică este furnizată explicit operatorului și nu este
descărcată din același canal cu update-ul.

Nu promite controlul tuturor parametrilor sau aplicare instantanee. Registrele de protecție a rețelei și parametrii instalatorului necesită o politică distinctă. La pierderea cloud-ului, acest subset nu modifică regimul invertorului.
