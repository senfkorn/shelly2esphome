# shelly2esphome

Flash **ESPHome over the air** onto Shelly Gen2 (ESP32) devices that still run the original
Shelly firmware. No need to open the case, no serial adapter, no soldering.

One Python file, standard library only. GUI and command line, English and German,
Windows and Linux.

*Deutsche Kurzanleitung: [weiter unten](#deutsch).*

> **Warning:** Once flashed, there is no way back to the Shelly firmware except via a
> serial adapter. A broken ESPHome build (wrong board settings, no Wi-Fi credentials)
> also means serial recovery. Read [ESPHome config](#esphome-config) first.

## Supported devices

| Model       | Status   |
|-------------|----------|
| Plus 2PM    | tested (Shelly FW 1.7.5 -> ESPHome 2026.9.1) |
| Plus 1      | untested, same partition table |
| Plus 1PM    | untested, same partition table |
| Plus I4     | untested, same partition table |
| Plus Plug S | untested, same partition table |
| Plus Uni    | untested, same partition table |

The Mini series (ESP32-C3) and Gen3/Gen4 devices are **not** supported.

## How it works

1. Checks the Shelly (Gen2, no password, supported model) and the firmware
   (valid ESP32 image, checksum, SHA-256, size, single/multi-core build).
2. Downgrades the Shelly to stock firmware 1.3.3, whose updater installs the package format
   below (including the bootloader). 1.3.3 boots "uncommitted" and rolls back after ~25 s,
   so the tool confirms it with `OTA.Commit` right away and verifies it with a reboot.
3. Builds an update package in Shelly's format: ESPHome bootloader + Shelly partition table
   + ESPHome app. The zip **must be stored uncompressed**, otherwise the Shelly silently
   rejects it.
4. Serves both packages from a built-in HTTP server, starts `Shelly.Update` and waits until
   the ESPHome API port (6053) answers.

The partition table stays Shelly's (2 x 1.5 MB OTA slots), so normal ESPHome OTA updates
work afterwards.

## Requirements

- Python 3.8 or newer. Nothing else.
- For the GUI on Linux: `sudo apt install python3-tk` (Debian/Ubuntu).
- PC and Shelly in the same network. The Shelly downloads the firmware **from your PC**,
  so a firewall must allow incoming connections (Windows asks on first start: allow
  "Private networks").
- The Shelly must not have a password set (disable it in the Shelly web UI first).

## Usage

### GUI

```bash
python3 shelly2esphome.py
```

1. **Device:** scan the network or type the IP address.
2. **Firmware:** choose the ESPHome `.bin` (ESPHome dashboard -> Install -> Manual download,
   preferably *Factory format*).
3. **Flash:** "Check", then "Flash ESPHome". A confirmation dialog shows device, model and
   both firmware versions.

The language switch is in the top right; the choice is saved in `~/.shelly2esphome.json`.

### Command line

```bash
python3 shelly2esphome.py 192.168.1.50 firmware.factory.bin
python3 shelly2esphome.py --scan 192.168.1.0/24
python3 shelly2esphome.py --help
```

| Option | Meaning |
|--------|---------|
| `--scan NET` | list Shellys in a network |
| `--build-only` | only build and save the package |
| `-y` | do not ask (a multi-core build additionally needs `--allow-multicore`) |
| `--lang de\|en` | language, default: system language |
| `--port N` | fixed port for the HTTP server (default: random) |
| `--shelly-zip FILE` | own copy of the Shelly 1.3.3 firmware |
| `--bootloader FILE` | custom bootloader (only for OTA-format firmware) |

Set `S2E_DEBUG=1` to log every HTTP request the Shelly makes.

## ESPHome config

Older hardware revisions have a **single-core** ESP32 rated for **160 MHz**. A firmware
built for dual-core or 240 MHz does not boot there. These settings run on all revisions:

```yaml
esp32:
  board: esp32dev
  framework:
    type: esp-idf
    sdkconfig_options:
      CONFIG_FREERTOS_UNICORE: y
      CONFIG_ESP_DEFAULT_CPU_FREQ_MHZ_160: y
      CONFIG_ESP_DEFAULT_CPU_FREQ_MHZ_240: n
```

The tool detects a firmware built without `CONFIG_FREERTOS_UNICORE` and warns prominently.
It cannot detect the CPU frequency setting.

Also make sure the config has working Wi-Fi credentials (and ideally `ap:` plus
`captive_portal:` as a fallback) and `ota:`, otherwise the device is unreachable after
flashing.

Firmware formats:
- **Factory format** (recommended): the bootloader from your build is used.
- **OTA format**: a bundled ESPHome 2026.9.1 bootloader (single-core, tested) is used.

## Shelly stock firmware 1.3.3

The tool needs Shelly firmware 1.3.3 for the downgrade. It is **not included** in this
repository; the tool downloads it from `http://rojer.me/files/shelly/stock/1.3.3/` when needed.
For offline use, put the files into `firmware/` next to the script (`<Model>-1.3.3.zip`) or pass
`--shelly-zip`. Every file is checked against a SHA-256 hash built into the tool.

## Troubleshooting

| Message | Cause / fix |
|---------|-------------|
| Shelly is password protected | Disable authentication in the Shelly web UI. |
| Shelly did not download the file | Firewall blocks incoming connections, or PC and Shelly are in different networks/VLANs. |
| Shelly rolled back to ... | `OTA.Commit` came too late. Just run the tool again. |
| Shelly rejected the package | The device still runs Shelly firmware, nothing is broken. Please open an issue with the log. |
| Device no longer responds | ESPHome started but cannot reach Wi-Fi. Look for the ESPHome fallback hotspot, otherwise flash via serial. |
| ... is already running ESPHome | Nothing to do, update via ESPHome as usual. |

## Development

```bash
python3 dev/test_shelly2esphome.py      # 35 tests, no real device needed
python3 dev/mockshelly.py --scenario normal
python3 shelly2esphome.py 127.0.0.1:18080 firmware.bin
```

`dev/mockshelly.py` simulates a Plus 2PM including the rollback without `OTA.Commit`, the
rejection of compressed zips and several error scenarios (`--scenario auth`, `no_fetch`,
`ignore_commit`, `reject_esphome`, `esphome_silent`, ...). The tests only talk to
`127.0.0.1` and block any RPC to other addresses.

## Credits

- The package format follows [mgos32-to-tasmota32](https://github.com/tasmota/mgos32-to-tasmota32).
- Shelly stock firmware archive hosted by rojer.me.

Not affiliated with Shelly (Allterco Robotics) or ESPHome / Open Home Foundation.
Use at your own risk.

## License

[GPL-3.0-or-later](LICENSE). The Shelly stock firmware is not part of this repository.

---

## Deutsch

ESPHome per OTA auf Shelly Gen2 (ESP32) flashen, ohne Aufschrauben und ohne seriellen Adapter.

**Start**
- Windows: Python 3 von python.org installieren, `shelly2esphome.py` doppelklicken.
  Beim ersten Flashen die Firewall-Abfrage für *private Netzwerke* zulassen.
- Linux: `sudo apt install python3-tk` (nur für die GUI), dann `python3 shelly2esphome.py`.

**Bedienung:** Netz durchsuchen oder IP eintragen, ESPHome-Firmware wählen (ESPHome-Dashboard
-> Install -> Manual download, am besten *Factory format*), „Prüfen“, dann „ESPHome flashen“.
Sprache oben rechts umschaltbar.

**Wichtig für die ESPHome-Config:** `CONFIG_FREERTOS_UNICORE: y` und 160 MHz setzen
(siehe [ESPHome config](#esphome-config)), sonst startet die Firmware auf älteren
Single-Core-Geräten nicht. Das Tool warnt bei Multicore-Builds.

**Achtung:** Zurück zur Shelly-Firmware geht danach nur noch seriell.
