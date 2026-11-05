# 🏠 Vesta/Climax Local Integration for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![GitHub Release](https://img.shields.io/github/v/release/B1oko/vesta-local-ha)](https://github.com/B1oko/vesta-local-ha/releases)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

A Home Assistant custom integration for **local control** of Vesta/Climax alarm panels (HSGW-MAX series and compatible models). This integration communicates directly with your panel over your local network - **no cloud required**.

> **Fork note**: this is a fork of [mphel44/vesta-local-ha](https://github.com/mphel44/vesta-local-ha) that adds camera photos, motion for wired PIRs from the event log and a few fixes (see [Changes in this fork](#-changes-in-this-fork)). The domain (`vesta_local`) and entity unique IDs are unchanged, so an existing installation keeps its entities when switching the HACS repository.

## 🙏 Acknowledgements

This integration is inspired by [koying's smarthomesec_ha](https://github.com/koying/smarthomesec_ha), an integration that talks to the Smarthomesec website. This version has been adapted to communicate locally with the panel instead. Thanks to koying for the original work!

## ✨ Features

- 🔒 **100% Local Control** - All communication stays on your local network
- ⚡ **Responsive** - 5-second polling interval for near real-time updates
- 🚨 **Alarm Control Panel** - Arm/Disarm in multiple modes (Away, Home, Night) - no code required
- 🔔 **Triggered State Detection** - Real-time alarm trigger detection via Contact ID events
- 📡 **Binary Sensors** - Door contacts, motion detectors, smoke/CO detectors, water leak sensors, glass break detectors
- 🔋 **Device Battery Monitoring** - Dedicated battery status sensor for each wireless device
- 📜 **Event Log** - Access the panel's event history
- 📋 **Per-device Event Log** - Each device gets a "Last Event" sensor with its full event history
- 📷 **Camera photos** - Last photo of each PIR camera as an `image` entity, optional saving to a folder and a `vesta_local_capture` event for notifications
- 🏃 **Wired PIR motion** - Motion detectors that never report a status turn on from the panel event log

## 📦 Installation

### HACS (Recommended)

1. Open HACS in Home Assistant
2. Click on "Integrations"
3. Click the three dots menu (top right) and select "Custom repositories"
4. Add this repository URL: `https://github.com/B1oko/vesta-local-ha`
5. Select "Integration" as the category
6. Click "Add"
7. Search for "Vesta/Climax Local" and install it
8. Restart Home Assistant

### Manual Installation

1. Download the latest release from the [releases page](https://github.com/B1oko/vesta-local-ha/releases)
2. Extract the `custom_components/vesta_local` folder to your Home Assistant `config/custom_components/` directory
3. Restart Home Assistant

## ⚙️ Configuration

### Automatic Discovery

If your panel supports Zeroconf/mDNS, Home Assistant will automatically detect it and prompt you to set it up.

### Manual Setup

1. Go to **Settings** > **Devices & Services**
2. Click **Add Integration**
3. Search for "Vesta/Climax Local"
4. Enter your panel's details:
   - **Host**: IP address or hostname of your panel (e.g., `192.168.1.100`)
   - **Username**: Panel username (default: `admin`)
   - **Password**: Panel password (default: `cX+HsA*7F1`)
   - **Use HTTPS**: Enable if your panel uses HTTPS (e.g., via a reverse proxy)

> **Note**: The default credentials can be found on page 9 of the [installation manual](https://www.scribd.com/document/715294461/Vesta-HSGW-MAX-20220330). The username and password can be changed later in the panel configuration webpage.

### Direct Connection (Default)

For direct connection to your panel on your local network:
- Leave "Use HTTPS" unchecked
- Enter the panel's local IP address (e.g., `192.168.1.100`)
- The integration will use HTTP on port 80

### Via Reverse Proxy

If you access your panel through a reverse proxy with HTTPS:
- Check "Use HTTPS"
- Enter your proxy hostname (e.g., `vesta.yourdomain.com`)
- The integration will use HTTPS

## 🚨 Alarm Control

The alarm control panel entity supports the following modes:
- **Disarm** - Disarm the alarm system
- **Arm Away** - Full arm mode
- **Arm Home** - Home arm mode (partial perimeter)
- **Arm Night** - Night arm mode
- **Triggered** - Automatically detected when an alarm is triggered

> **Note**: No code is required to arm or disarm the alarm through this integration. The panel authenticates via HTTP Basic Auth with your configured credentials.

### Triggered State Detection

The integration monitors reported events from the panel to detect alarm triggers in real-time. The alarm state changes to **Triggered** when:
- The most recent event has `new_event == "Trigger"`
- The Contact ID code (`cid_event`) is one of: `130` (Burglary), `131` (Perimeter), or `132` (Interior)

The triggered state automatically resets when a new event occurs (disarm or restore).

## 📷 Camera Photos

PIR cameras (device type 27, "IR Camera") take a burst of photos (6 × 320×240 JPEG on a Hybrid Panel Lite) when the alarm triggers. The photos reach the panel over the radio 10 to 20 seconds after the trigger and are kept in `/temp/download/`, which the panel loses when it restarts.

- **Image entity** `Last photo` per camera (unique ID `<entry_id>_<device_id>_last_capture`): the last photo of the most recent capture. Attributes: `time`, `type`, `status`, `zone`, `area`, `photo_count`, `files` (panel paths), `photo` (the one shown) and `saved_files`.
- **Polling**: the capture list (`captureEventListGet`) is read every 60 s, and every 5 s for 90 s after a new alarm event in the event log or reported events, until the capture is `Done`.
- **Saving (option, off by default)**: in the integration options enable *Save camera photos to a folder*. Default folder: `<config>/media/vesta` (`/config/media/vesta` in the container). Layout: `<folder>/<YYYY-MM-DD>/<HHMMSS>_zone<N>_<n>.jpg`. Already saved files are remembered (in `.storage/vesta_local.captures_<entry_id>`), so photos are not saved twice after restarts even though the panel reuses its capture ids. To browse them in **Media**, add the folder to `media_dirs`:

  ```yaml
  homeassistant:
    media_dirs:
      local: /media
      vesta: /config/media/vesta
  ```

- **Event** `vesta_local_capture`, fired once per capture when all its photos have arrived (or after 2 minutes with the photos that arrived). Captures already on the panel when Home Assistant starts, or older than 15 minutes, do not fire it. Data: `entry_id`, `device_id`, `area`, `zone`, `time`, `type`, `status`, `photo_count`, `files`, `saved_files` and `entity_id` (the image entity).

Example notification with the photo:

```yaml
automation:
  - alias: Alarm photo
    triggers:
      - trigger: event
        event_type: vesta_local_capture
    actions:
      - action: notify.mobile_app_my_phone
        data:
          title: Alarm
          message: "Camera zone {{ trigger.event.data.zone }} at {{ trigger.event.data.time }}"
          data:
            image: "/api/image_proxy/{{ trigger.event.data.entity_id }}"
```

The integration never deletes captures from the panel.

## 🏃 Wired PIR Motion

Some panels (e.g. Hybrid Panel Lite) never report a `status` for wired PIRs in `deviceListGet`. Such devices are reported as `off` instead of `unknown`, and motion sensors turn `on` for 30 s after an event log entry with action `Response` for their zone (`Zone1(Comedor)`, `Zone3`...; messages such as `Burglar Alarm`, `Follow`, `Start Entry 1`). The panel only logs these while armed, so a disarmed panel shows no motion. The last such entry is in the `last_log_motion` and `last_log_motion_msg` attributes.

## 🎛️ Supported Devices

### Alarm Panel
- Climax/Vesta HSGW-MAX series
- Other Vesta-compatible panels with local HTTP API

### Sensors (automatically detected)
| Device Type | Home Assistant Entity |
|-------------|----------------------|
| 🚪 Door Contact | Binary Sensor (Door) |
| 🏃 PIR / IR Motion | Binary Sensor (Motion) |
| 📷 IR Camera (PIR camera) | Binary Sensor (Motion) + Image (last photo) |
| 🔥 Smoke Detector | Binary Sensor (Smoke) |
| 💨 CO Detector | Binary Sensor (CO) |
| 💧 Water Leak Sensor | Binary Sensor (Moisture) |
| 🪟 Glass Break Detector | Binary Sensor (Vibration) |

### Diagnostic Sensors
- 📶 GSM Signal Strength (%)
- 🔋 Panel Battery Status
- 🔌 AC Power Status
- 📜 Event Log (last event + 20 most recent events in attributes)
- 📋 Per-device Last Event (last action + 20 most recent device events in attributes)
- 🪫 Per-device Battery Status (Low Battery alerts)

## 🛠️ Technical Details

### API Endpoints

| Endpoint | Description |
|----------|-------------|
| `/action/panelCondGet` | Get panel status (mode, battery, GSM signal) |
| `/action/deviceListGet` | Get list of all enrolled devices |
| `/action/panelCondPost` | Set alarm mode |
| `/action/logsGet` | Get event history (POST with `max_count`) |
| `/action/reportEventListGet` | Get reported events for triggered state detection (POST with `max_count`) |
| `/action/captureEventListGet` | Get camera captures and their photo files (POST with `max_count`) |
| `/temp/download/<file>.jpg` | Capture photo (same Basic auth) |

### Polling

- Default interval: 5 seconds
- Panel status, device list, and event log are fetched concurrently for efficiency
- Exponential backoff retry logic for network resilience

## 🔀 Changes in this Fork

**1.3.0**
- Camera photos: `image` entity per PIR camera, optional saving to a folder (options flow) and `vesta_local_capture` event.
- Idle devices with an empty status are `off` instead of `unknown` (upstream PR #7 by DistinctMotives), and motion sensors turn on from the event log.
- PIR cameras ("IR Camera") get a motion binary sensor.
- Zone devices are linked to the panel with `via_device_id` (the `via_device` tuple is deprecated and stops working in HA 2027.8).
- The plain-text "Access Denied: Session expired!" answer is retried.
- Options flow keeps the options; credentials are only re-tested when they change.
- Spanish translation of the new options and entity.

## 🧪 Tests

```bash
pip install pytest-homeassistant-custom-component
pytest
```

## 🤝 Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## ⚠️ Disclaimer

This integration is not affiliated with, endorsed by, or connected to Climax Technology or Vesta. All product names, logos, and brands are property of their respective owners.
