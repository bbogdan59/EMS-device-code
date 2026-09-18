# Raport de validare: profilul candidat Deye SG04LP3 (issue #1)

Acest document insoteste `profiles/deye_sg04lp3_candidate.json`. Explica exact
ce a fost verificat in aceasta sesiune de lucru si ce ramane strict in sarcina
operatorului, cu hardware real, inainte de a seta `"verified": true`.

## De ce profilul ramane `verified: false`

Conform README si `readers.ModbusReader`, `verified=true` este o **atestare a
operatorului**, nu o certificare software. Nu am avut acces la un invertor
Deye fizic in aceasta sesiune (mediu de dezvoltare, fara adaptor USB-RS485
conectat), deci profilul candidat NU poate fi marcat verificat aici, indiferent
cat de solida e sursa documentara. Codul (`ModbusReader`) refuza oricum sa
citeasca orice profil cu `verified != true` -- profilul candidat e inert pana
un operator il valideaza si il comuta explicit.

## Ce este confirmat (verificabil fara hardware)

- **Adresele, functia (3), encoding-ul (u16/s16/u32) si scalele** sunt
  transcrise exact din `kbialek/deye-inverter-mqtt` (Apache-2.0), commit
  `6762eaa5927f1aefc060eef090c54bf60fcaab9d` (2026-09-06):
  `docs/metric_group_deye_sg04lp3.md`, `docs/metric_group_deye_sg04lp3_battery.md`,
  `docs/metric_group_deye_sg04lp3_ups.md`, `src/deye_sensors_deye_sg04lp3.py`.
  Acest proiect e activ intretinut, cu CI si o comunitate mare de utilizatori
  Deye reali -- cel mai bun substitut disponibil pentru documentatia oficiala
  a producatorului in acest mediu (fetch HTTP direct catre `deyeinverter.com`
  a fost blocat de proxy-ul de retea al sesiunii).
- **Familia de model** (`SUN-5/6/8/10/12K-SG04LP3-EU`, harta Modbus identica
  pe toata familia per sursa de mai sus) e confirmata pe pagina oficiala de
  produs: https://deye.com/product/sun-5-6-8-10-12k-sg04lp3-eu/ -- acopera
  variania 10K folosita de statia de referinta a platformei.
  vezi si `docs/LIMITATIONS.md`/catalogul din EMS-management-platform, unde
  modelul "Deye SUN-10K-SG04LP3" e deja listat ca profil de dispozitiv.
- **Blocurile de citire** (`"blocks"` din profil) acopera STRICT adresele
  individual documentate de sursa de mai sus -- niciun bloc nu "traverseaza"
  registre nedocumentate (ex. 589 intre 588 si 590 e exclus deliberat din
  orice bloc; vezi testul `test_block_never_reads_past_declared_length`).
- **Mecanismul de decodare** (u16/s16/u32/s32, ordinea cuvintelor, offset,
  blocuri, campuri calculate, readiness-check) e acoperit de
  `tests/test_readers.py` cu valori sintetice dar realiste, inclusiv un test
  dedicat care incarca EXACT fisierul `deye_sg04lp3_candidate.json` si verifica
  autoconsistenta schemei (fiecare punct acoperit de exact un bloc, fara
  campuri necunoscute/duplicate, referinte `computed` rezolvabile).

## Ce NU este confirmat -- verifica obligatoriu pe hardware real inainte de `verified: true`

1. **Sensul `battery_power_w`/`battery_current_a` (scale -1 / -0.01 in profil).**
   Sursa comunitara nu documenteaza explicit conventia de semn a
   `Battery Power` (registrul 590/591) fata de conventia platformei
   (`app/models/telemetry.py` din EMS-management-platform: pozitiv=incarcare,
   negativ=descarcare). Am inversat semnul in profil PRIN ANALOGIE cu un
   pattern confirmat live in aceeasi sesiune, pe alt canal: API-ul Deye Cloud
   (`batteryPower`) foloseste pozitiv=descarcare/negativ=incarcare -- opusul
   platformei. E plauzibil ca Deye foloseasca aceeasi conventie si la nivel de
   registru Modbus local (adesea cloud-ul reflecta direct registrele), dar
   NU e o certitudine pentru acest canal. **Verifica: incarca vizibil
   bateria si confirma ca `battery_power_w` iese pozitiv; descarca vizibil si
   confirma ca iese negativ.** Daca e invers, schimba `scale` din `-1`/`-0.01`
   in `1`/`0.01` pentru ambele campuri.
2. **Sensul `grid_power_w` (registrul 625, "Total Grid Power").** Lasat cu
   `scale: 1` (nicio inversare), presupunand aceeasi conventie ca platforma
   (pozitiv=import). Neconfirmat pe acest canal local. **Verifica: cu
   consum > productie (import de retea) confirma pozitiv; cu export
   confirma negativ.**
3. **Combinatia `signed=true` + `offset=-100.0` pentru `dc_temperature_c`/
   `ac_temperature_c`** (registrele 540/541). Sursa comunitara foloseste
   exact aceasta combinatie neobisnuita (fata de `battery_temperature_c`,
   care e `unsigned` + acelasi offset) -- transcrisa fidel, nu "corectata" pe
   presupuneri proprii. **Verifica la o temperatura camerei cunoscuta ca
   valoarea decodata e plauzibila** inainte de a avea incredere in ea sub 0°C.
4. **`grid_ct_l1/l2/l3_w` (registrele 604-606, "Internal CT").** Eticheta
   sursei ("Internal CT") sugereaza CT-ul intern al invertorului, posibil
   diferit ca semantica exacta de `grid_power_w` (registrul 625, deja
   canonic). Nu presupune ca cele trei valori insumate egaleaza
   `grid_power_w` fara sa confirmi pe hardware.
5. **Parametrii seriali** (`modbus_serial_reference`: 9600/N/1) sunt cei
   documentati de sursa pentru aceasta familie de protocol, NU cititi de pe
   eticheta/DIP-switch-urile unitatii reale. Confirma in documentatia
   exacta a unitatii/manualul de instalare inainte de a le folosi in
   `config.toml`.
6. **Niciun sentinel de indisponibilitate nu e implementat in profilul
   candidat** (ex. o valoare gen `0xFFFF` insemnand "indisponibil" pe vreun
   registru). Sursa comunitara nu documenteaza un asemenea sentinel pentru
   familia SG04LP3; mecanismul de profil suporta totusi -- vezi mai jos --
   un camp opozitional viitor daca se confirma unul pe hardware real. Pana
   atunci, orice valoare bruta e tratata ca reala, chiar daca ar fi
   neplauzibila (plafonul de plauzibilitate server-side ramane singura plasa
   de siguranta, vezi `docs/LIMITATIONS.md` din EMS-management-platform,
   issue #118).
7. **`readiness_check` (registrul 500, "Running status") a fost gandit sa
   respinga devreme un profil incompatibil**, dar acceptand orice cod din
   {0,1,2,3,4} conform enum-ului documentat de sursa -- nu a fost niciodata
   citit dintr-un invertor real in aceasta sesiune.

## Cum valideaza un operator

1. Instaleaza agentul pe Raspberry Pi cu adaptorul RS485 conectat la
   invertorul Deye SG04LP3 real (`reader="modbus"` in configuratie,
   `profile` indicand o COPIE locala a `deye_sg04lp3_candidate.json`, INCA
   `verified: false`).
2. Cu agentul oprit, ruleaza manual (script/REPL, nu serviciul systemd) o
   citire directa cu `pymodbus` peste blocurile din profil si compara
   valorile decodate cu afisajul local al invertorului/aplicatia oficiala
   Deye pentru fiecare punct de mai sus, in special cele 4 semnalate.
3. Corecteaza `scale`/`offset`/`word_order` in COPIA locala daca oricare nu
   corespunde, documentand ce s-a schimbat si de ce (numarul de serie/
   firmware-ul exact al unitatii testate, in `model`/`firmware` din profil).
4. Abia dupa acest pas, seteaza `"verified": true` in copia locala -- NU in
   `deye_sg04lp3_candidate.json` din acest repository, care ramane sablonul
   de pornire nevalidat pentru urmatorul operator/model.
