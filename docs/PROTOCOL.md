# Contract v0.1 și extensii

## Compatibilitate curentă

Contract inspectat în `bbogdan59/EMS-management-platform`, baza inițială `d1426b9`. API-ul folosit este cel existent, nu endpoint-uri propuse:

| Operație | Endpoint | Comportament agent |
|---|---|---|
| Enrollment | POST /api/v1/devices/enroll | UUID + secret de provisioning + serial + cod de activare; pending până la claim-ul clientului |
| Configurație | GET /api/v1/config | cache config_version/preference_version; nu confirmă aplicarea în invertor |
| Prezență | POST /api/v1/devices/heartbeat | boot_id, firmware_version agent, telemetry=true, inverter_write=false |
| Telemetrie | POST /api/v1/telemetry/batch | ACK per item: șterge accepted/duplicate, păstrează retryable, mută permanent în dead-letter; fallback sigur la răspunsul agregat vechi |
| Rotație credential | POST /api/v1/devices/credentials/rotate | issue #3: autentificat cu secretul CURENT; serverul revocă imediat vechiul secret și întoarce unul nou o singură dată (`api.rotate_credential`, CLI `rotate-credential`) |

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
- `unavailable_values` (opțional): maximum 32 de valori brute unsigned, pe lățimea punctului (16/32 biți), confirmate în protocolul modelului. Compararea are loc după combinarea cuvintelor, înainte de semn, scalare și offset. Punctul indisponibil este omis; zero rămâne o valoare reală, fără sentinele implicite.

Opțional, profilul poate declara:

- `blocks`: listă de `{function, start, length}` -- registre citite într-un singur apel Modbus în loc de unul per punct. Fiecare bloc trebuie să acopere STRICT adrese deja documentate de puncte (nicio "traversare" a unei zone nedocumentate doar ca să unească două puncte apropiate); blocurile nu se pot suprapune în aceeași funcție; lungimea e limitată la `MAX_BLOCK_REGISTERS=60` (sub limita Modbus de 125, ca să reducă riscul unui cadru RS485 lung pe o magistrală/adaptor zgomotos). Registrele FC03 și FC04 au spații de adrese separate: aceeași adresă este permisă în ambele și valorile nu se suprascriu. Fără `blocks`, comportamentul rămâne cel din v0.1 (un apel per punct).
- `computed`: câmpuri derivate ca sumă a altor puncte deja citite (ex. `pv_power_w` = `pv1_power_w` + `pv2_power_w`, pentru că Deye SG04LP3 nu expune un registru unic de putere PV totală). Fiecare termen din `sum_of` trebuie să fie un punct deja definit, fără dubluri. Dacă lipsește un termen, suma este omisă integral. `quality_flags.unavailable_fields` enumeră punctele și sumele omise în mostra parțială; dacă toate câmpurile lipsesc, nu se generează o mostră goală sau zero.
- `readiness_check`: `{field, allowed_values}` -- citit O SINGURĂ DATĂ, înainte de bucla periodică de eșantionare (`ModbusReader.check_ready()`, apelat din `cli.py` imediat după construirea reader-ului). Citește numai punctul necesar sau termenii sumei, chiar dacă profilul declară blocuri mai mari. Respinge valori indisponibile sau în afara listei numerice finite. Un status plauzibil singur nu identifică modelul/firmware-ul.
- `identity_checks`: maximum 16 verificări read-only `{function, address, expected_registers}`, exclusiv FC03/FC04, fiecare cu 1..16 registre brute unsigned de 16 biți. Adresele și valorile exacte de model/firmware trebuie documentate și validate de operator. Verificările rulează înainte de readiness și telemetrie, apoi sunt memorate pentru conexiunea curentă. După eroare, închidere ori reconectare sunt repetate; nepotrivirea oprește citirea telemetriei. Nu există scanare sau scriere de registre. Fără acest câmp nu se pretinde identificarea automată a modelului.

Nu există adrese demonstrative care ar putea fi confundate cu registre DEYE reale. Profilul minim are 1..64 puncte. Mecanismele `unavailable_values` și `identity_checks` sunt testate cu fixture-uri sintetice; profilul candidat livrat nu declară sentinele sau registre de identitate neconfirmate -- vezi `docs/VALIDATION_SG04LP3.md`. Un profil local greșit poate produce valori plauzibile: validarea hardware rămâne obligatorie, iar `verified: true` este o atestare a operatorului dupa acea validare, niciodata a codului/agentului.

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

Inventarul aplicației folosește câmpurile deja existente din contractul
platformei #168 (inspectat la `030a8245cd92bcdf8bafc49adf60f572bc4d095e`):
`agent_version` la enrollment, `firmware_version` la heartbeat, plus `build_id`,
`hardware_platform`, `architecture`, `os_version` la ambele. `health` expune
aceleași observații. `firmware_version` este versiunea aplicației EMS, nu a
invertorului. Nu sunt trimise câmpuri OTA inventate; polling-ul/confirmarea
ofertelor fleet rămân urmărite în device #13.

Pașii 1, 2, 4 și modul sigur sunt implementați în agent. Claim-ul self-service
din pasul 3 este contractul comun cu `EMS-management-platform#44`. Identificarea
DEYE (issue #1) și desired/reported complet rămân work items separate.

## Recuperare după revocare/transfer/factory-reset (issue #3)

`EMS-management-platform` are UI de admin pentru revoke/transfer/factory-reset
(`device_service.revoke_device`/`transfer_device`/`factory_reset_device`), dar
NICIUNA dintre aceste acțiuni are un canal push către device -- agentul nu are
niciun endpoint de tip "notificare". Singurul semnal pe care device-ul îl
poate observa este un `401`/`403` la următorul `heartbeat`/`config`/`telemetry`,
pentru că serverul a revocat deja credentialul curent.

Recuperarea e deliberat MANUALĂ, niciodată automată (un 401 tranzitoriu -- bug
server, ceas nesincronizat -- nu trebuie să distrugă o asociere încă validă):

- Agentul se oprește (`CredentialInactiveError`, `SystemExit(1)`); systemd îl
  repornește, dar bucla de enrollment (`while not state.get("credentials")`)
  NU se reactivează singură cât timp `credentials` locale există, chiar dacă
  serverul le-a revocat -- fără intervenție ar rezulta o buclă de crash
  infinită cu un credential mereu invalid.
- Operatorul rulează `ems-device ... reset --confirm-serial <serial>`
  (`State.clear_assignment`): șterge `credentials`/`enrollment_status`/
  `platform_origin` local, emite un Device Code nou (cel vechi e deja
  consumat/compromis), PĂSTREAZĂ `installation_uuid`/`serial_number`/
  `provisioning_secret` -- aceeași unitate fizică, gata de re-enrollment către
  o stație (sau chiar un `platform_url`) nou. În aceeași tranzacție se șterge
  politica stației din cache și se mută outbox-ul în dead-letter cu motivul
  `assignment_reset`; backlog-ul nu poate trece la tenantul următor.
- `reset --factory` (`State.factory_reset`) e pentru hardware repus în
  circuit pentru alt client: șterge și coada/dead-letter locale și emite o
  identitate COMPLET nouă -- nimic din instalarea anterioară nu mai e
  reutilizabil, simetric cu regula "nicio identitate în imaginea OS".
- Ambele cer `--confirm-serial` EXACT egal cu serialul curent (`identity`) --
  fără potrivire, comanda refuză și nu schimbă nimic local.

`accept_enrollment_response` respinge explicit (`assignment_identity_mismatch`)
un răspuns "assigned" pentru un device/station DIFERIT de cel deja persistat
local, ca plasă de siguranță suplimentară față de un race/replay ("două
conturi" din criteriile de acceptare) -- deși în fluxul normal acest cod nu e
niciodată atins din nou după ce `credentials` există (nici `run`, nici
`provision` nu re-apelează `enroll()` în acel caz).

Rotația de credential (`rotate-credential`) marchează local
`credential_rotation_pending=true` ÎNAINTE de cererea de rețea; dacă răspunsul
se pierde, agentul NU poate distinge "rotația a reușit server-side dar am
pierdut confirmarea" de "a fost efectiv revocat" -- deci nu reîncearcă orbește
cu vechiul secret. Flag-ul rămâne vizibil în `health` până la următoarea
rotație reușită sau un `reset`, care rezolvă oricare din cele două posibilități
uniform.

## Distribuirea release-urilor

Release-urile nu conțin `/var/lib/ems-device` și sunt instalate sub
`/opt/ems-device/releases/<version>`. Un manifest minisign verificat leagă
versiunea de URL-ul HTTPS și SHA-256-ul arhivei. Activarea schimbă atomic
`/opt/ems-device/current`; serviciul systemd folosește exclusiv acel symlink.
Preflight-ul deschide SQLite read-only ca `ems-device`, fără lock exclusiv,
enrollment sau RS485. Venv-ul nu este mutat după creare. Un restart urmat de
verificări `systemctl is-active` timp de cinci secunde nereușite reactivează
release-ul anterior. Jurnalul root-owned `update-state.json` este persistat
înainte de switch. Watchdog-ul separat confirmă numai un PID systemd activ cu
boot_id nou, release și versiune exacte și dovadă de contact reușit cu platforma
din acel proces. Pending enrollment poate confirma după un răspuns autorizat
pending; un device asociat confirmă după config și heartbeat reușite.
Timeout-ul monotonic de 120 secunde sau schimbarea boot-ului OS înainte de
confirmare produc rollback. Evenimentele fleet ale platformei rămân separate.
Installerul, reset-ul configuratorului și updaterul exclud concurența printr-un
lock de deployment separat de lock-ul agentului. Cheia publică este furnizată explicit operatorului și nu este
descărcată din același canal cu update-ul.

Nu promite controlul tuturor parametrilor sau aplicare instantanee. Registrele de protecție a rețelei și parametrii instalatorului necesită o politică distinctă. La pierderea cloud-ului, acest subset nu modifică regimul invertorului.

## Identitate hardware și imagini clonate (P0 #3)

Serialul public `EMS-...` rămâne aleator, generat local o singură dată; nu este
credential și nu se schimbă la upgrade sau soft reset. Pe Raspberry Pi,
agentul citește modelul și serialul din Device Tree (`/sys/firmware/devicetree/base`
sau `/proc/device-tree`). Sursa serialului este documentată de
[Raspberry Pi](https://www.raspberrypi.com/documentation/computers/raspberry-pi.html).
Serialul hex este normalizat și hash-uit cu un prefix de sursă; fingerprint-ul
public este salvat separat de secretele de provisioning.

La prima rulare pe o unitate existentă fără binding, fingerprint-ul observat
este adoptat fără a schimba serialul/UUID-ul sau credentialele. Ulterior,
`provision`, `run`, rotația și soft reset verifică binding-ul înainte de orice
apel API ori acces RS485. Un fingerprint diferit sau dispariția unei surse deja
legate produce `HardwareIdentityError` și păstrează starea pentru diagnostic.
`identity`/`health` afișează `matched`, `mismatch`, `unbound` sau `unavailable`,
plus fingerprint-urile publice, fără secrete.

Hardware-ul fără o sursă suportată păstrează identitatea software unică și
raportează `unavailable`; nu deducem hardware din hostname, MAC sau machine-id.
Nu există attestation/secure element: un operator root poate falsifica metadatele,
iar o clonă făcută înainte de primul binding nu poate fi recunoscută retrospectiv.
Imaginile OS trebuie în continuare livrate fără `/var/lib/ems-device`.

Mutarea intenționată a SD-ului către altă placă necesită `reset --factory
--confirm-serial <serial>`, cu serviciul oprit. Aceasta schimbă toate secretele,
șterge datele vechi și leagă noua identitate de placa observată într-o singură
tranzacție. Soft reset păstrează binding-ul și nu permite reutilizarea secretelor
unei imagini clonate. Seria absentă pe o placă deja legată cere repararea sursei
hardware sau reprovisioning explicit, nu regenerare automată de identitate.

O rotație cu rezultat necunoscut nu mai poate fi retrimisă prin aceeași comandă;
`credential_rotation_pending` cere intervenție. Recuperarea idempotentă a
secretului după un răspuns pierdut necesită încă extensia contractului platformei.
