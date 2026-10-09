# Gen3 / Gen4: experimental testing

## Deutsch

**Noch nicht durch dieses Projekt auf echter Hardware getestet.** Der Gen2-Pfad bleibt unverändert. Gen3/Gen4 werden ausschließlich über den separaten Experimental-Dialog oder `--experimental` aktiviert. Erste Profile: **Shelly Plug S Gen3 (`PlugSG3`, ESP32-C3, 8 MB)** und **Shelly 2PM Gen4 (`S2PMG4`, ESP32-C6, 8 MB)**. Das ist keine Freigabe aller Geräte derselben Generation. Unbekannte Modelle können bereits Diagnoseberichte liefern.

OTA-Konvertierung ersetzt die Herstellerfirmware und den Bootloader. Ein Fehler kann serielle Rettung erfordern. Nur testen, wenn diese Möglichkeit besteht. Der verfügbare API-Port ist ein Hinweis, kein Nachweis eines erfolgreichen Bootloaders oder funktionierender zukünftiger Updates. Zigbee/Matter der Herstellerfirmware werden durch diesen Konverter nicht zu ESPHome-Funktionen.

### Ablauf für Tester

1. Aktuellen Quellcode verwenden. Die ältere v1.0.0-EXE enthält diese Funktionen noch nicht; neue EXEs werden vom Windows-Release-Workflow gebaut.
2. Im GUI `Gen3 / Gen4 (experimental)` öffnen. IP eintragen und zunächst **Diagnose** ausführen. Das ist rein lesend und braucht keine Firmwaredatei. Alternativ:

   ```sh
   python shelly2esphome.py --cli DEVICE_IP --diagnose --report diagnose.json
   ```

3. Das offizielle Shelly-Update-ZIP für **genau dieses Modell und die aktuell installierte Firmwareversion** lokal bereitstellen, beispielsweise über den offiziellen Shelly-Updater. Das Tool lädt für neue Generationen keine Archive herunter und führt keinen Gen2-Downgrade durch. Modell, Version und vorhandene Prüfsummen werden geprüft. Ein lokal ausgewähltes ZIP gilt nicht als kryptographisch verifizierte Herstellerdatei; nur aus offizieller Quelle verwenden.
4. ESPHome **factory BIN** mit dem passenden C3/C6-Bootloader bauen. Eine normale OTA-BIN reicht hier nicht. Das vollständige Stock-Partitionslayout übernehmen: Bootloader bei `0x0`, Partitionstabelle bei `0x10000`, `otadata` bei `0x11000`, App bei `0x20000`. Beide OTA-Slots und alle Datenpartitionen müssen exakt dem Stock-ZIP entsprechen. Für PlugSG3: App-Slot `0x2a0000`; für S2PMG4: `0x300000`. Eine verkleinerte oder generische ESPHome-Partitionstabelle wird abgelehnt.

   Als Ausgangspunkt für die ESPHome-Konfiguration (Geräte-Pins separat konfigurieren):

   ```yaml
   esp32:
     board: esp32-c3-devkitm-1 # S2PMG4: esp32-c6-devkitm-1
     variant: ESP32C3         # S2PMG4: ESP32C6
     flash_size: 8MB
     partitions: stock-partitions.csv
     framework:
       type: esp-idf
       sdkconfig_options:
         CONFIG_PARTITION_TABLE_OFFSET: '0x10000'
       advanced:
         enable_ota_rollback: false
   api:
   ota:
     - platform: esphome
   ```

   `stock-partitions.csv` lässt sich im GUI mit **Export CSV** erzeugen, bevor eine Factory-BIN vorliegt, oder mit:

   ```sh
   python shelly2esphome.py --cli DEVICE_IP --experimental --shelly-zip stock.zip --export-partitions stock-partitions.csv --report layout.json
   ```

   Der Export ist rein lesend. GPIO-Belegung, Flash-Modus und Siliziumrevision müssen zur Hardware passen. Die Prüfung vergleicht diese binären Eigenschaften, erkennt aber keine falschen GPIOs oder fehlende Schutzfunktionen. Eine vollständige Gerätekonfiguration mit Temperatur-/Leistungsschutz selbst prüfen. Es wird kein eingebetteter Gen2-Bootloader verwendet.
5. Im GUI **Preflight** ausführen oder:

   ```sh
   python shelly2esphome.py --cli DEVICE_IP firmware.factory.bin --experimental --shelly-zip stock.zip --check --report preflight.json
   ```

   Dieser Befehl verändert das Gerät nicht. Auch **Save ZIP** / `--build-only` schreibt nur eine lokale Datei. ZIP und BIN können eingebettete Zugangsdaten enthalten: nicht veröffentlichen.
6. Erst nach erfolgreicher Prüfung **Flash (experimental)** wählen oder `--check` im obigen Befehl weglassen. Die CLI verlangt die Eingabe `FLASH`; `--yes` ist die bewusste nichtinteraktive Freigabe. Vor dem Update wird UDP-Debugging kurz eingeschaltet, ausschließlich der Hinweis `Storing core dumps to app_0` ausgewertet und die ursprüngliche UDP-Adresse wiederhergestellt und kontrolliert. Ohne eindeutiges Ziel **Slot 0** wird kein Update gestartet. `GetDeviceInfo.slot` allein genügt nicht. UDP muss vom Gerät zum Rechner durch die Firewall gelangen.
7. Bei Slot 1: mit dem offiziellen Shelly-Verfahren die Stock-Firmware aktualisieren bzw. erneut installieren, danach erneut prüfen. Das Tool erzwingt keinen Slot-Wechsel. Bleibt die Anzeige unklar, Diagnose als Issue einreichen. Bei `debug_restore_failed` die vorherige UDP-Debug-Adresse im Shelly wiederherstellen; das Tool flasht dann nicht.
8. Nach Konvertierung tatsächliche ESPHome-API-Verbindung prüfen, Gerät neu starten, **zwei unterschiedliche neue ESPHome-Builds nacheinander per normalem ESPHome-OTA installieren**, jeweils Boot und Funktionen prüfen. Erst diese Ergebnisse erlauben eine Bewertung des künftigen OTA-Pfads. Das Tool führt die beiden Folgetests nicht automatisch aus. Im Experimental-Dialog die Ergebnisse manuell wählen und den JSON-Bericht exportieren. Den Dialog dafür offen lassen; Ergebnisse können auch im Issue ergänzt werden.
9. Über [Experimental-Test-Issue](https://github.com/senfkorn/shelly2esphome/issues/new?template=experimental-test.yml) berichten. Fehlgeschlagene und blockierte Versuche helfen ebenfalls. Niemand wird automatisch benachrichtigt; der Export bleibt lokal, bis der Tester ihn selbst anhängt.

### Was der Bericht enthält

Schema-Version, Werkzeugname, Experimental-Markierung, bekannte Profilkennung, Generation, plausibel formatierte Stock-Version, Prüfstatus, SHA-256 der beiden Eingabedateien, Chip/Offsets/Größen, erkannter Zielslot, Ablaufstatus und manuell gesetzte Testergebnisse. **Keine** IP, MAC, Gerätenamen, Pfade, WLAN-Zugangsdaten, API-Schlüssel, YAML-Inhalte, rohen RPC-Antworten, Fehlertexte oder rohen Logs. Auch Fehler erzeugen mit `--report` einen Bericht. Vor dem öffentlichen Anhängen selbst durchsehen.

`--test-result reboot=passed --test-result ota1=failed` kann manuelle Ergebnisse an einen CLI-Bericht anhängen. Diese Angaben sind Selbstauskünfte, keine automatischen Messungen. Ein erfolgreicher Bericht stuft ein Profil nicht automatisch als getestet ein.

### Bewusste Grenzen

Der Paketbau übernimmt Stock-Datenpartitionen, ersetzt Bootloader/App und setzt `boot.min_version` auf `1.0.9` nach dem öffentlich dokumentierten Konvertierungsansatz. Stock-Pakete mit höherem/unbekanntem Mindest-Bootloader werden blockiert. Es gibt keinen Hardware-Nachweis, dass jede Firmware diesen Bootloader tatsächlich ersetzt. Signierte/verschlüsselte Images, abweichende Partitionen, höhere erforderliche Chiprevisionen und widersprüchliche Slot-Hinweise werden abgelehnt. Enhanced-Security-/HTTPS-only-Geräte sind mit dem vorhandenen lokalen HTTP-Server nicht unterstützt; Sicherheitseinstellungen werden nicht abgeschaltet.

## English

**These profiles have not been tested on physical hardware by this project.** Use the separate experimental GUI window or `--experimental`; the Gen2 path is unchanged. Only PlugSG3 (ESP32-C3, 8 MB) and S2PMG4 (ESP32-C6, 8 MB) have package profiles. Unknown devices support read-only diagnosis only.

Start with `--diagnose --report diagnostic.json`. Supply an official stock ZIP matching both the exact device model and installed version, and an ESPHome factory BIN with the complete stock layout (boot `0x0`, PT `0x10000`, app `0x20000`, both OTA slots and all data partitions). Use ESP-IDF, an 8 MB target, `CONFIG_PARTITION_TABLE_OFFSET: '0x10000'`, and disable ESPHome rollback. The YAML above shows build settings only; supply and verify the actual device pins and protection functions yourself.

`--check` and `--build-only` never write to the device. Flashing temporarily configures UDP logging, reads only the core-dump target-slot indication, restores and verifies the original logging destination, and requires unambiguous target slot 0. Otherwise it aborts before issuing an update. Slot 1 requires a manual official stock update/reinstallation before trying again. No automatic downgrade, security disabling or forced slot override is provided. Serial recovery may be required after a failed conversion.

The package uses the public experimental bootloader-replacement approach with minimum bootloader version `1.0.9`; replacement is not proven by a TCP listener. The input ZIP is locally supplied and is not independently authenticated by the tool. Signed/encrypted images, incompatible layouts or chip revisions, and unknown/newer bootloader minima are blocked. Devices requiring HTTPS update transport are unsupported by this HTTP-based test path.

After conversion verify a real ESPHome API connection, reboot/power-cycle, then install **two distinct subsequent ESPHome OTA builds**, checking reboot and hardware functions after each. Enter manual results in the GUI, export JSON and attach it to the [test issue](https://github.com/senfkorn/shelly2esphome/issues/new?template=experimental-test.yml). Keep the experimental window open to retain the session. The tool does not run those hardware tests or automatically promote support status.

Reports use a strict field whitelist, including model profile, generation, stock version, artifact hashes, layout, stage/guard outcomes and manual test results. No addresses, identifiers, credentials, file paths, configuration contents, raw logs or exception messages are exported. Nothing is uploaded automatically. Firmware BIN/ZIP may contain secrets: do not attach them. Old v1.0.0 binaries do not contain this feature; use current source or a newly built Windows artifact.

## Sources / technical basis

Independent implementation based on publicly documented format and conversion behavior:

- [Shelly Sys RPC and UDP debug configuration](https://shelly-api-docs.shelly.cloud/gen2/ComponentsAndServices/Sys/)
- [free-shelly-ota PlugSG3 notes](https://github.com/oxynatOr/free-shelly-ota/blob/main/docs/devices/PlugSG3.md), [partition layout](https://github.com/oxynatOr/free-shelly-ota/blob/main/partitions/PlugSG3-stock.csv), [target-slot probing](https://github.com/oxynatOr/free-shelly-ota/blob/main/shelly_ota/sender.py)
- [shelly-gen4-esphome partition documentation](https://github.com/automatous-io/shelly-gen4-esphome/blob/main/docs/PARTITIONS.md) and [package format](https://github.com/automatous-io/shelly-gen4-esphome/blob/main/scripts/make-esphome-ota-zip.py)

Public success reports for another converter are not hardware validation of this tool. Extend profiles only with exact chip/layout information and test evidence, not merely a generation number.
