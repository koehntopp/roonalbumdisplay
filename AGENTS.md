# esp32_rgb — Agent Notes

Technical details and decisions accumulated while building this project.
`README.md` covers usage; this file covers *why*, plus pitfalls worth not
re-discovering.

## Hardware

- **Controller:** Adafruit Matrix Portal M4 (SAMD51, 120MHz, CircuitPython).
  Has an onboard ESP32 co-processor for WiFi (driven via `adafruit_esp32spi`,
  not native networking — the M4 itself has no WiFi).
- **Panel:** a 64x64 RGB LED matrix, 3mm pitch, labeled "P3-HS260324-1000".
  This exact part number has no findable public datasheet — likely a
  generic/unbranded panel. Its actual pin assignments were never confirmed
  against a vendor datasheet; what's used here (see "Address E jumper"
  below) came from Adafruit's own board defaults plus empirical testing.
- May move to a bare ESP32-S3 later (discussed, not started — see "Possible
  future direction" below).
- Board's LAN IP is whatever your DHCP server assigns it (not statically
  reserved in this setup) — check its serial console output or the panel's
  own IP status screen.
- Roon Core's LAN address is never hardcoded anywhere; `roon_client/*.py`
  rediscover it via SOOD multicast on every run.

## HUB75 connector: the 20-pin socket and the Address E jumper

The Matrix Portal M4's onboard HUB75 socket is a **2x10 (20-pin)** header,
not the generic 16-pin HUB75 standard. It's sized so a standard 2x8
(16-pin) panel ribbon seats flush against one edge, with 4 pins
overhanging unused on the other side. (Source: Adafruit's own
[pinouts guide](https://learn.adafruit.com/adafruit-matrixportal-m4/pinouts).)

The board also has an **"Address E" solder jumper** on its underside. 64x64
matrices need 5 row-address lines (A-E) instead of 4, and different panel
manufacturers put that 5th line (E) on different physical pins of their
16-pin connector — commonly either pin 8 or pin 16. The jumper selects
which of those two pins the board's E signal comes out on:

- Ships from Adafruit **pre-set to pin 8**, which matches Adafruit's own
  64x64 matrices.
- Third-party panels (like this one) may need pin 16 instead — check the
  panel's own datasheet/silkscreen.
- Changing it means melting solder to move the bridge from one pad to the
  other — a real hardware modification, not something to guess at without
  checking the panel's actual pin-out first.

As of this writing the jumper is confirmed still bridged to **pin 8**
(unmodified, factory default). It was never conclusively established
whether this panel actually needs pin 16 — the color-tint fault we chased
(see below) turned out to be a cable-seating issue instead, and once fixed
the display worked correctly at the pin-8 default. If a *geometry* fault
shows up later (rows split/mirrored/duplicated, as opposed to a solid
color tint), the Address E jumper is the next thing to suspect.

## The color-tint hardware fault (resolved)

Early testing showed a **solid, content-independent color tint** on the
panel:

1. First panel: the **top half glowed blue** even when commanded to
   display an all-zero (fully black) frame via `POST /clear` — proof it
   was electrical, not a code or content issue, since an all-zero
   framebuffer has nothing for any code bug to mis-render.
2. After swapping to a second panel (same ribbon cable reused): a
   magenta/cyan color-mixing pattern on a test image, then a solid
   near-full-red tint on real album art with no red dominance.

**Diagnostic technique worth keeping:** `POST /clear` sends a real
all-zero 12288-byte frame through the exact same rendering path as a real
image. If a color artifact survives an all-black frame, it cannot be a
software/content bug — the panel is receiving stuck/incorrect signal
regardless of data. This immediately separates "hardware/wiring fault"
from "code bug" without needing to read a single line of `render()`.

**Root cause:** the ribbon cable was not seated fully flush against the
correct edge of the Matrix Portal's 20-pin socket (see above — a 16-pin
cable on a 20-pin header has a specific correct alignment, and being off
by even one row scrambles which physical pin carries which signal). A
different offset produced a different "stuck" color each time it was
reseated, which is why the fault's color changed between attempts. A
careful, deliberate reseat — flush on both the Matrix Portal's 20-pin
header and the panel's 16-pin socket — resolved it completely.

Lesson: don't jump to "defective panel" or "wrong jumper" when a *solid
color tint* — as opposed to geometric/row distortion — shows up on two
different panels with the same symptoms. Check cable seating first.

**Power:** the panel runs from a separate dedicated 5V supply, not the
Matrix Portal's USB-passthrough terminals (which Adafruit documents as
output-only: USB power flows through them, they don't accept external
power in).

## `device/code.py` (board firmware)

Runs as an HTTP server on port 80 on the board itself:

- `POST /image` — raw 64x64 RGB888 body (12288 bytes), renders it.
- `POST /clear` — blanks the panel (also the hardware-fault diagnostic
  above).
- `POST /settings?brightness=0-1&contrast=0-4&gamma=0.1-5` (any subset),
  `GET /settings` — read current values.
- `GET /status` — width/height/image_bytes/has_image/free_mem.

Key implementation decisions:

- **RGBMatrix wiring** (`rgb_pins`, `addr_pins` with all 5 lines A-E,
  `clock_pin`/`latch_pin`/`output_enable_pin` via `board.MTX_*`) is
  Adafruit's own canonical example for this board+panel-size pairing —
  not something to second-guess without a specific reason.
- **`bit_depth`**: raised from the initial `4` to **`6`**. At `bit_depth=4`
  the panel only has 16 PWM brightness levels per color channel; combined
  with dimming the display via software (multiplying pixel values down
  before quantization — see below), this caused visible color banding at
  low brightness. `bit_depth=6` gives 64 levels/channel. Confirmed no
  visible flicker at 6 on this board — if a future board/panel combo does
  flicker at 6, back off to 5 first.
- **Brightness/contrast/gamma** are applied via a shared 256-entry lookup
  table (`build_lut()`) computed once per settings change, applied
  identically to R, G, and B before RGB565 packing. This means dimming
  happens *before* the RGB565 quantization step, which is what makes the
  `bit_depth` interaction above relevant — it's not a fully independent
  hardware dimming path.
- **`BLACK_FLOOR` clamp in `render()`**: RGB565 gives green 6 bits but red
  and blue only 5. Near-black JPEG compression noise (a handful of
  nonzero levels the codec introduces around pure-black regions) can
  survive in green after the LUT while rounding to true zero in red/blue,
  producing a visible **green tint on what should be pure black**. Fixed
  by checking `r < 8 and g < 8 and b < 8` (8 being where red/blue already
  round to zero) and forcing the pixel to true 0 in that case, rather than
  packing it through the asymmetric bit masks. This only affects pixels
  that would already be crushed to zero in two of three channels, so it
  doesn't visibly clip legitimate dark colors.
- **Default brightness is `0.1`** (`settings` dict default), chosen after
  live A/B testing at 1.0 → 0.5 → 0.3 → 0.2 → 0.1 against a real P3 panel,
  which is very bright by spec (1000+ cd/m² is typical for this panel
  class). If you want it configurable without editing code, that'd need
  a `settings.toml` value or a small state file on the CIRCUITPY drive.
- **`build_lut()` must be called at boot, not just from `/settings`.**
  Found the hard way: `apply_settings()` (the `POST /settings` handler)
  was the *only* caller of `build_lut()`. The `settings` dict's default
  values were real, but inert - `lut` stayed the raw identity table
  (`bytearray(range(256))`, i.e. full brightness) at every boot/reload
  until something actually POSTed to `/settings`. So every code deploy
  (which reloads the board) came back at full brightness, and it only
  ever looked right because brightness got manually re-POSTed afterward
  in this session - not because the default worked. Fixed by calling
  `build_lut()` once at module load, right after its definition. If you
  ever add more settings that feed into a derived table/state like this,
  make sure the derivation runs at boot from the defaults, not only from
  the HTTP handler that updates it.
- **The WiFi/IP status text bypasses the brightness system entirely** -
  `status_label` is a `displayio` `Label` with a hardcoded raw color
  (`0x00FF00`), rendered through `displayio`'s own text path, not through
  `render()`/`lut` at all. Fixed by setting `status_label.color` inside
  `build_lut()` too (`lut[255] << 8` - conveniently, `lut[255]` equals
  `brightness*255` exactly regardless of contrast/gamma, since the
  contrast/gamma curve pins to 1.0 at the top of the input range), so the
  status screen dims consistently with images instead of always showing
  at full intensity while WiFi is connecting or no image has been pushed
  yet.
- **Watchdog + crash recovery**: `microcontroller.watchdog` is armed
  (`WatchDogMode.RESET`, `WATCHDOG_TIMEOUT = 16`s — the max SAMD51 allows;
  it only accepts power-of-2 timeouts up to 16s) right before the first
  WiFi connect attempt, fed once per main-loop iteration and once per
  `connect()` retry-loop iteration. A genuine hang anywhere resets the
  board within 16s instead of leaving the display frozen indefinitely.
  Separately, `if __name__ == "__main__"` wraps `main()` in a try/except
  that logs any unhandled exception and calls `microcontroller.reset()` —
  belt-and-suspenders self-healing for a 24/7-unattended display, since
  neither the watchdog nor the crash handler existed before.
- **ESP32SPI listener-recycling quirk**: the `adafruit_esp32spi` socket
  driver hands the *listening* socket itself to an accepted client
  connection (rather than a distinct client socket), so the server must
  create a fresh listener whenever the accepted client's socknum matches
  the listener's — handled in `main()`'s loop. Without this, the server
  stops accepting new connections after the first request.
- WiFi credentials live in `settings.toml` on the CIRCUITPY drive itself
  (not in this repo — only `settings.toml.example` is checked in). 2.4GHz
  only, per CircuitPython's ESP32SPI limitations on this board.

**Sync discipline:** `device/code.py` in this repo and
`/Volumes/CIRCUITPY/code.py` on the board are two separate files with no
automatic sync — every firmware change needs an explicit
`cp device/code.py /Volumes/CIRCUITPY/code.py`. CircuitPython auto-reloads
and restarts the board a few seconds after the file changes, which resets
its in-memory state (`has_image` goes back to `False`, `settings` resets
to the coded defaults — `settings.toml` itself is untouched). Expect the
board to be briefly unreachable during that reload, and expect one
occasional slow/timing-out first `POST /image` right after a reload
(follow-up requests are fast, ~0.3s) — this has been observed as benign
board-side settling, not a new fault, unless it starts happening
persistently.

## `roon_client/` (Mac-side Python)

Roon Labs doesn't publish a Python SDK — their official SDK
(`node-roon-api` + companion modules) is Node.js. `roon_client` uses the
third-party PyPI package **`roonapi`** (aka "pyroon", v0.1.6 at time of
writing), which reimplements the same LAN discovery (SOOD multicast
protocol on port 9003) and websocket extension protocol as the official
SDK. It's what Home Assistant's own Roon integration is built on.

- **`pair.py`** — one-time authorization. Discovers core(s) via
  `RoonDiscovery(None).all()`, creates a non-blocking `RoonApi` per
  candidate, polls until one gets a `.token` (set once the user clicks
  "Enable" on the extension in Roon > Settings > Extensions), then
  persists `roon_core_id.txt` + `roon_token.txt` next to the script
  (gitignored). Needs to actually run and wait — see the background-task
  pitfall below for how *not* to launch it.
- **`roon_to_matrix.py`** — the daemon. Loads saved credentials,
  rediscovers the core's current `host:port` via
  `RoonDiscovery(core_id).first()` (handles the Core's IP changing),
  connects with `blocking_init=True`, and registers a state callback on
  `zones_changed`/`zones_seek_changed`.
- **Album art path**: `RoonApi.get_image(image_key, scale='fit', width=64,
  height=64)` returns a URL to Roon Core's *own* `/api/image/<key>`
  endpoint — Core does the fetching/resizing/caching, the extension just
  builds the URL. Fetched with `requests`, decoded with Pillow, letterboxed
  onto a black 64x64 canvas (`thumbnail` + centered `paste`) so non-square
  art doesn't distort, then sent as raw RGB888 to the board.

### Zone-selection logic (rewritten twice — read this before touching it again)

This setup has multiple Roon zones (a living-room zone, and two others,
one of which is what's actually used day-to-day). The "which zone do we
show" logic went through two real bugs before landing on the current
design:

1. **v1 (broken):** picked literally the first zone in Roon's reported
   dict, once, at startup, and never looked again. Roon's dict order is
   arbitrary and has nothing to do with which zone is playing — this
   showed music-is-playing-but-panel-is-blank whenever the first zone
   happened to be idle.
2. **v2 (still broken):** re-evaluated on every state-change event;
   prefer any zone with `state == 'playing'`, else fall back to a paused
   zone with art, else clear. This *fixed* v1's bug but introduced a new
   one: Roon reports the zone you're actually listening to as briefly
   `"loading"` (not `"playing"`) *between tracks*. During that instant, no
   zone matched `'playing'`, so the code fell through to the paused-zone
   fallback and grabbed a **different, unrelated zone** that happened to
   be sitting paused on old content — visibly flashing stale album art on
   every single track change. (Observed concretely: a zone sat paused all
   session on an old track, and its art kept flashing in between every
   track change on the actually-playing zone.)
3. **v3 (current):** sticky zone tracking via a `state['shown'] =
   (zone_id, image_key)` tuple. Once a zone is being displayed, a brief
   non-`'playing'` blip (`state in ('loading', 'paused')`) *in that same
   zone* just holds the current frame (a `KEEP` sentinel — no panel
   update at all) instead of switching away. The arbitrary-paused-zone
   fallback only fires when **nothing has ever been shown yet** (cold
   start with no zone actively playing). The panel only actually switches
   zones when the tracked zone truly stops, or a genuinely different zone
   starts playing.

If this needs further work (e.g. handling simultaneous multi-room
playback more thoughtfully, or a zone that pauses for a long time and
should eventually clear rather than hold forever), start from the v3
design above rather than re-deriving zone selection from scratch.

### Known transient issue (not fixed, just observed)

`roonapi`'s bundled `roonapisocket.py` hit
`AttributeError: 'WebSocketApp' object has no attribute 'decode'` once
during a normal session — an apparent version mismatch between what
`roonapi`'s `on_message` handler expects and the installed
`websocket-client` release's callback signature. It logged the error,
dropped the socket, and reconnected on its own ~20s later via `roonapi`'s
built-in retry logic, then kept working normally. Left unfixed since it
self-recovered; if it starts recurring frequently, pinning an older
`websocket-client` version is the likely fix.

### Robustness for unattended/24-hour operation

Prompted by a real question mid-session ("do we need error handling and
memory management for all-day operation?"). Current state:

- **`run_once()`/`main()` split**: `main()` now wraps a `run_once()`
  connect-and-watch session in an outer `while True` retry loop
  (`RETRY_SECONDS = 15`). Any unexpected exception - not just the two
  network calls (`fetch_art`, `push_image`) that were already
  individually try/excepted - logs and reconnects instead of killing the
  whole daemon. `KeyboardInterrupt` still exits cleanly.
- **`on_state_change` is explicitly guarded** (try/except around
  `refresh()`) rather than relying on `roonapi`'s own dispatch loop to
  swallow bugs in *our* callback too - it happened to catch the one real
  `roonapi`-internal crash observed (see above), but that's `roonapi`
  protecting itself, not a guarantee about code we register with it.
- **Board-side** got a hardware watchdog and top-level crash recovery -
  see `device/code.py` section above.
- **Deliberately declined**: a persistent macOS LaunchAgent for
  `roon_to_matrix.py` (auto-start on login, auto-restart on crash/reboot).
  Asked and the user chose to keep running it manually. This means: if
  the Mac restarts, sleeps in a way that kills the process, or the
  process crashes in a way `run_once()`'s retry loop doesn't catch (e.g.
  the Python interpreter itself segfaults), nothing brings the client
  back except manually re-running
  `uv run roon_client/roon_to_matrix.py --panel-url http://<board-ip>`.
  If this becomes annoying, revisit — the plist would be straightforward
  to add later.
- Brightness/contrast/gamma are only settable live via `/settings`; the
  `0.1` default lives in code, not in a user-editable config.

## Possible future direction: ESP32-S3

Discussed but not started: replacing the Matrix Portal M4 with a bare
ESP32-S3 board (WROOM-1-N16R8 module, e.g. an ESP32-S3-DevKitC-1) for
more CPU/RAM headroom and native WiFi. If this happens:

- Needs 14 free GPIOs for HUB75 (R1/G1/B1/R2/G2/B2/A/B/C/D/E/CLK/LAT/OE)
  plus a HUB75 breakout — no onboard connector like the Matrix Portal has.
- On the N16R8 module specifically, avoid GPIO35-37 (used by the octal
  PSRAM), GPIO0/3/45/46 (strapping pins), GPIO19/20 (native USB D-/D+ if
  using that port), GPIO43/44 (UART0/serial-flash port).
- Would need its own dedicated 5V supply for the panel (same as now).
- Would most likely move off CircuitPython entirely, toward
  `ESP32-HUB75-MatrixPanel-DMA` (Arduino/PlatformIO) rather than
  CircuitPython's `rgbmatrix` module — CircuitPython support for HUB75 on
  S3 boards exists but wasn't evaluated for this specific hardware.
- `device/code.py`'s HTTP API (`/image`, `/clear`, `/settings`,
  `/status`) is deliberately host-agnostic — the `roon_client/` side
  wouldn't need to change at all, only the firmware.

## Operational pitfalls hit during development (harness/tooling, not the project itself)

These aren't project decisions, but they cost real time to debug and are
worth not re-learning:

- **Never nest your own `&` background operator inside a
  harness-managed background command.** Doing
  `some_long_running_script.py &` inside a command already run with
  background execution orphans the child process when the wrapping shell
  exits — it can get killed before it finishes (this happened to the
  first `pair.py` attempt, which was silently killed before the user
  could even see the Roon "enable extension" prompt). Just background the
  bare command directly and let the harness manage it.
- **Python's stdout is block-buffered when not attached to a TTY.** A
  backgrounded `uv run some_script.py` will appear to produce no output
  for a long time even though it's running correctly — the `print()`
  calls are sitting in a buffer. Run with `PYTHONUNBUFFERED=1` whenever
  you need to tail a background task's log file for live diagnosis.
