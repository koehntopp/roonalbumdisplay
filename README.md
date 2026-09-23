# roonalbumdisplay

Shows the album art of whatever's currently playing on a local [Roon](https://roon.app)
Core on a 64x64 RGB LED matrix panel, driven by an Adafruit Matrix Portal M4
running CircuitPython.

A small Python daemon on your Mac (or any always-on machine on the same
network) watches Roon's now-playing state, fetches the current track's
album art from Roon Core, and pushes it over WiFi to the panel.

## How it works

```
Roon Core  <---LAN--->  roon_client/roon_to_matrix.py  ---WiFi HTTP--->  Matrix Portal M4  --->  64x64 panel
```

- `roon_client/roon_to_matrix.py` registers as a Roon extension, watches
  for now-playing changes, fetches the current track's art from Roon
  Core's own image API, resizes/letterboxes it to 64x64, and POSTs it as
  raw RGB888 to the board's own tiny HTTP server.
- `device/code.py` runs on the Matrix Portal M4 itself: it joins your WiFi
  and serves a small HTTP API (`/image`, `/clear`, `/settings`, `/status`)
  that renders whatever image it's given onto the panel.

See [AGENTS.md](AGENTS.md) for the full technical history — hardware
faults hit and fixed, firmware design decisions, and the Roon
zone-selection logic (which went through a couple of real bugs before
landing on its current design). This README covers getting it running.

## Hardware build

### Components

- [Adafruit Matrix Portal, CircuitPython Powered Internet Display, Cortex M4 (#4745)](https://www.amazon.de/dp/B0DXD9YR9K) —
  the controller. Has WiFi via an onboard ESP32 co-processor, a HUB75
  connector for the panel, and screw terminals for panel power.
- [64x64 RGB LED Matrix Panel, 192x192mm, 3mm pitch, 4096 LEDs](https://www.amazon.de/dp/B0BYJHMFSQ) —
  the display itself.
- A separate 5V power supply for the panel, rated for a few amps (a 64x64
  panel can draw several amps at full brightness/full white — don't rely
  on the Matrix Portal's USB power for this; its screw terminals are
  output-only, passing through USB power, not accepting external input).
- A HUB75 ribbon cable to connect the two (usually included with the
  panel).

### Assembly

1. **Connect the ribbon cable** between the Matrix Portal's HUB75 socket
   and the panel's HUB75 **input** socket (panels usually have two — input
   and output, for daisy-chaining; make sure you're on the input side).

   The Matrix Portal's socket is a 20-pin (2×10) header, sized so a
   standard 16-pin (2×8) ribbon seats flush against one edge with 4 pins
   overhanging unused on the other side. **Seat it fully and flush** — if
   it's off by even one row, colors on the panel will be wrong (a
   solid, content-independent color tint is the telltale sign; see
   AGENTS.md for how this was diagnosed and fixed in practice).

2. **Check the Address E jumper.** 64x64 panels need a 5th row-address
   line ("E") that different panel manufacturers wire to different pins
   of the connector — usually pin 8 or pin 16. The Matrix Portal M4 has a
   solder jumper on its underside to select which; it ships set to pin 8,
   which matches Adafruit's own 64x64 panels. Check your panel's
   documentation (or its PCB silkscreen near the connector) for which pin
   it actually uses, and re-solder the jumper if it needs pin 16 instead.
   Getting this wrong typically shows up as row/geometry distortion
   rather than wrong colors.

3. **Wire the panel's power input** to your separate 5V supply (not to the
   Matrix Portal). Make sure the supply's ground is common with the
   Matrix Portal's ground (they usually already are, through the ribbon
   cable's ground pins, but double check with a multimeter if unsure).

4. **Power on** the 5V supply first (or simultaneously), then connect the
   Matrix Portal to your computer via USB.

### If something looks wrong

A **solid color tint** that persists even on an all-black test frame
(`POST /clear`, see below) is a wiring/cable-seating fault, not a
software issue — reseat the ribbon cable, flush on both ends. **Row or
geometry distortion** (mirrored/duplicated/split rows) points at the
Address E jumper instead. See `AGENTS.md`'s "color-tint hardware fault"
section for the full diagnostic story.

## Software installation

### 1. Flash CircuitPython onto the board

Download CircuitPython for the `matrixportal_m4` board from
[circuitpython.org](https://circuitpython.org/board/matrixportal_m4/).
Double-tap the board's reset button to enter the bootloader (a
`MATRIXBOOT` drive should appear), then drag the downloaded `.uf2` file
onto it. The board reboots and a `CIRCUITPY` drive appears.

> If `MATRIXBOOT` doesn't appear on macOS: try a direct USB port (no hub),
> a different cable, and make sure nothing else (e.g. a browser tab using
> WebUSB/WebSerial) has an open connection to the board — any of these
> can prevent the mass-storage drive from mounting. Flashing from a
> different computer and then plugging the already-flashed board back in
> also works if a specific machine won't cooperate.

### 2. Install CircuitPython libraries

Using [`circup`](https://learn.adafruit.com/keep-your-circuitpython-library-bundle-up-to-date/circup-install)
(`pip install circup`, then `circup install <name>` with the `CIRCUITPY`
drive mounted), install:

```
adafruit_matrixportal
adafruit_display_text
adafruit_bitmap_font
adafruit_esp32spi
adafruit_connection_manager
adafruit_requests
adafruit_portalbase
adafruit_fakerequests
adafruit_ticks
neopixel
simpleio
```

### 3. Deploy the firmware

Copy this repo's `device/code.py` onto the `CIRCUITPY` drive:

```bash
cp device/code.py /Volumes/CIRCUITPY/code.py
```

Then create `settings.toml` **directly on the CIRCUITPY drive** (not in
this repo) with your WiFi credentials, based on
`device/settings.toml.example`:

```toml
CIRCUITPY_WIFI_SSID = "your-ssid"
CIRCUITPY_WIFI_PASSWORD = "your-password"
```

The board only supports 2.4GHz WiFi. CircuitPython auto-reloads `code.py`
whenever it's saved, so the board restarts and connects automatically.
Its IP address is shown on the panel itself once connected (and printed
to its serial console).

Quick check once it's up:

```bash
curl http://<board-ip>/status
```

### 4. Pair with Roon

This repo's `roon_client/` scripts need [`uv`](https://docs.astral.sh/uv/)
(they're standalone scripts with inline PEP 723 dependencies — `uv run`
installs what they need automatically, no virtualenv setup required).

Run the one-time pairing script:

```bash
uv run roon_client/pair.py
```

It discovers your Roon Core on the LAN and waits. Open Roon, go to
**Settings > Extensions**, and click **Enable** on "Matrix Portal Now
Playing". Once approved, `pair.py` saves `roon_client/roon_core_id.txt`
and `roon_client/roon_token.txt` next to itself (gitignored — these are
your local credentials, never commit them).

### 5. Run the display client

```bash
uv run roon_client/roon_to_matrix.py --panel-url http://<board-ip>
```

By default it follows whichever Roon zone is actually playing. To pin it
to one specific zone regardless of what else is playing elsewhere:

```bash
uv run roon_client/roon_to_matrix.py --panel-url http://<board-ip> --zone "Living Room"
```

This runs in the foreground; stop it with Ctrl-C. Running it this way on
a Mac isn't a persistent background service (see AGENTS.md for why a
LaunchAgent was considered and declined) — for always-on unattended
operation, deploy it with Docker to another machine on the LAN instead;
see below.

## Deploying with Docker

`roon_client/` can run as a container on any other machine on your LAN
(a Linux server, a NAS, a Pi) instead of running ad hoc on a Mac —
Docker's `restart: unless-stopped` gives you the auto-restart-on-crash
and auto-start-on-boot behavior that running it manually doesn't.

**The one thing that matters**: `RoonDiscovery` finds your Roon Core via
UDP multicast broadcast on the LAN, which does not cross into a
container's default bridge network. `compose.yaml` sidesteps this
entirely by setting `ROON_HOST`/`ROON_PORT` to connect directly to your
Roon Core's known address, skipping discovery — this needs only a plain
outbound TCP connection, which works from default bridge networking on
any Docker host (unlike `--network host`, which only works properly on
Linux Docker hosts, not Docker Desktop on Mac/Windows). If your Roon
Core's LAN IP might change, give it a DHCP reservation in your router.

`deploy.sh` targets a [Dockge](https://github.com/louislam/dockge) stack
directory (`/opt/stacks/<name>/`) on the remote host — adjust `REMOTE_DIR`
if you're not using Dockge; any directory `docker compose` can run from
works the same way.

1. Pair locally first if you haven't already (`uv run roon_client/pair.py`
   — see above). The resulting `roon_client/roon_core_id.txt` and
   `roon_token.txt` aren't tied to a specific machine, just to this app's
   identity as approved in Roon, so they can be copied to the deploy
   target rather than re-paired there.
2. Edit `compose.yaml`'s `ROON_HOST` and `PANEL_URL` to match your setup.
3. Edit `deploy.sh`'s `REMOTE_USER`/`REMOTE_HOST`/`REMOTE_DIR` for your
   target machine. It builds the image locally for `linux/amd64` (adjust
   if your server is arm64, e.g. a Raspberry Pi), copies it and the
   compose file/credentials over SSH, and runs `docker compose up -d`
   there. The target machine needs Docker already installed — the script
   doesn't install it.
4. Run `./deploy.sh`. Check on it afterward with:
   ```bash
   ssh youruser@yourhost 'cd /opt/stacks/roonalbumdisplay && docker compose logs -f'
   ```

To redeploy after any code change, just run `./deploy.sh` again.

## Board HTTP API reference

- `POST /image` — raw 64x64 RGB888 body (exactly 12288 bytes), displays it
- `POST /clear` — blanks the panel (also useful as a hardware diagnostic —
  see above)
- `POST /settings?brightness=0-1&contrast=0-4&gamma=0.1-5` — any subset of
  these query parameters
- `GET /settings` — current brightness/contrast/gamma
- `GET /status` — width, height, expected image size, whether an image is
  currently shown, and free memory

## Repository layout

- `device/` — firmware for the Matrix Portal M4 (mirrors what's deployed
  to its CIRCUITPY drive; `settings.toml` with your WiFi credentials
  lives only on the board itself, never in this repo)
- `roon_client/` — the Mac/host-side Python daemon, one-time pairing
  script, and `Dockerfile`
- `compose.yaml`, `deploy.sh` — Docker deployment to another machine on
  the LAN (see above)
- `AGENTS.md` — full technical history: hardware faults diagnosed and
  fixed, firmware design decisions, the Roon zone-selection logic and its
  prior bugs, and known rough edges

## Possible future direction

Adafruit's newer [Matrix Portal S3](https://www.adafruit.com/product/5778)
would likely be a better fit for this exact use case going forward:
native WiFi (no separate co-processor, and no need for this project's
socket-recycling workaround), a standard 16-pin HUB75 socket (removing
the exact class of cable-seating fault this build ran into), and more
CPU/RAM headroom — while keeping the same plug-and-play HUB75 connector
and CircuitPython support that make the M4 easy to work with in the
first place. See AGENTS.md for the fuller comparison.
