import gc
import os
import time
from array import array

import bitmaptools
import board
import busio
import displayio
import framebufferio
import microcontroller
import rgbmatrix
import terminalio
from adafruit_display_text.label import Label
from adafruit_esp32spi import adafruit_esp32spi
from adafruit_esp32spi.adafruit_esp32spi_socketpool import SocketPool
from digitalio import DigitalInOut
from microcontroller import watchdog
from watchdog import WatchDogMode

# SAMD51's hardware watchdog only accepts power-of-2 timeouts up to 16s.
# Fed once per main-loop iteration and once per WiFi-(re)connect attempt, so
# a genuine hang (anywhere) or a WiFi outage longer than this triggers a
# full board reset instead of leaving the display frozen indefinitely.
WATCHDOG_TIMEOUT = 16

WIDTH = 64
HEIGHT = 64
IMAGE_BYTES = WIDTH * HEIGHT * 3
PORT = 80
USAGE = (
    "POST /image  body = raw 64x64 RGB888 (12288 bytes)\n"
    "POST /clear\n"
    "POST /settings?brightness=0-1&contrast=0-4&gamma=0.1-5   (any subset)\n"
    "GET  /settings\n"
    "GET  /status\n"
)
LIMITS = {b"brightness": (0.0, 1.0), b"contrast": (0.0, 4.0), b"gamma": (0.1, 5.0)}

displayio.release_displays()
matrix = rgbmatrix.RGBMatrix(
    width=WIDTH,
    height=HEIGHT,
    bit_depth=6,
    rgb_pins=[
        board.MTX_R1,
        board.MTX_G1,
        board.MTX_B1,
        board.MTX_R2,
        board.MTX_G2,
        board.MTX_B2,
    ],
    addr_pins=[
        board.MTX_ADDRA,
        board.MTX_ADDRB,
        board.MTX_ADDRC,
        board.MTX_ADDRD,
        board.MTX_ADDRE,
    ],
    clock_pin=board.MTX_CLK,
    latch_pin=board.MTX_LAT,
    output_enable_pin=board.MTX_OE,
)
display = framebufferio.FramebufferDisplay(matrix, auto_refresh=True)

bitmap = displayio.Bitmap(WIDTH, HEIGHT, 65536)
image_group = displayio.Group()
image_group.append(
    displayio.TileGrid(
        bitmap,
        pixel_shader=displayio.ColorConverter(input_colorspace=displayio.Colorspace.RGB565),
    )
)

status_label = Label(terminalio.FONT, text="", color=0x00FF00, x=1, y=8)
status_group = displayio.Group()
status_group.append(status_label)

pixels = array("H", [0]) * (WIDTH * HEIGHT)
body = bytearray(IMAGE_BYTES)
has_image = False
settings = {b"brightness": 0.1, b"contrast": 1.0, b"gamma": 1.0}
lut = bytearray(range(256))


def build_lut():
    contrast = settings[b"contrast"]
    gamma = settings[b"gamma"]
    brightness = settings[b"brightness"]
    for i in range(256):
        v = min(max((i / 255 - 0.5) * contrast + 0.5, 0.0), 1.0)
        lut[i] = min(int(v**gamma * brightness * 255 + 0.5), 255)
    # lut[255] is exactly `brightness*255` regardless of contrast/gamma (v
    # pins to 1.0 at i=255), so it doubles as the scale factor for the
    # status text's raw full-intensity green, keeping it as dim as images.
    status_label.color = lut[255] << 8


build_lut()  # apply the settings defaults immediately - without this call,
# `lut` stays the raw identity table (i.e. full brightness) at boot and on
# every reload, no matter what `settings` says, until a /settings POST
# happens to trigger a rebuild.


def show_status(text):
    if has_image:
        return
    status_label.text = text
    display.root_group = status_group


BLACK_FLOOR = 8  # below this, R and B already round to 0 in RGB565 (5-bit);
# clamp G (6-bit) to match, or near-black JPEG noise shows as a green tint.

# 4x4 Bayer ordered-dither matrix. RGB565 truncates R/B to 5 bits and G to
# 6 (8-step and 4-step rounding respectively) regardless of `bit_depth`,
# which only controls PWM/refresh, not color resolution - so smooth
# gradients (skies, skin tones) band visibly without this. Adding a
# small position-dependent offset before truncating spreads the rounding
# error across neighboring pixels, which reads as a smoother gradient.
DITHER_4X4 = (
    (0, 8, 2, 10),
    (12, 4, 14, 6),
    (3, 11, 1, 9),
    (15, 7, 5, 13),
)


def render():
    global has_image
    data = body
    table = lut
    j = 0
    i = 0
    for y in range(HEIGHT):
        drow = DITHER_4X4[y & 3]
        for x in range(WIDTH):
            r = table[data[i]]
            g = table[data[i + 1]]
            b = table[data[i + 2]]
            if r < BLACK_FLOOR and g < BLACK_FLOOR and b < BLACK_FLOOR:
                pixels[j] = 0
            else:
                d = drow[x & 3] - 8  # -8..7: about one 5-bit quantization step
                r = min(max(r + d, 0), 255)
                g = min(max(g + (d >> 1), 0), 255)  # G's step is half as coarse
                b = min(max(b + d, 0), 255)
                pixels[j] = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
            i += 3
            j += 1
    bitmaptools.arrayblit(bitmap, pixels)
    has_image = True
    display.root_group = image_group


def apply_settings(query):
    new = dict(settings)
    for pair in query.split(b"&"):
        if not pair:
            continue
        key, _, value = pair.partition(b"=")
        if key not in LIMITS:
            return "unknown setting: " + key.decode()
        try:
            number = float(value.decode())
        except ValueError:
            return "not a number: " + key.decode()
        low, high = LIMITS[key]
        if not low <= number <= high:
            return "%s must be between %s and %s" % (key.decode(), low, high)
        new[key] = number
    settings.update(new)
    build_lut()
    if has_image:
        render()
    return None


def settings_json():
    return '{"brightness":%.3f,"contrast":%.3f,"gamma":%.3f}\n' % (
        settings[b"brightness"],
        settings[b"contrast"],
        settings[b"gamma"],
    )


def respond(client, status, text="", ctype="text/plain"):
    payload = text.encode()
    header = "HTTP/1.1 {}\r\nContent-Type: {}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n"
    client.send(header.format(status, ctype, len(payload)).encode() + payload)


def handle(client):
    client.settimeout(5)
    chunk = bytearray(512)
    data = b""
    while b"\r\n\r\n" not in data:
        n = client.recv_into(chunk)
        data += chunk[:n]
        if len(data) > 2048:
            respond(client, "431 Request Header Fields Too Large", "headers too large\n")
            return
    head, _, rest = data.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    request = lines[0].split(b" ")
    if len(request) < 2:
        respond(client, "400 Bad Request", "bad request line\n")
        return
    method = request[0]
    path, _, query = request[1].partition(b"?")

    length = 0
    expect_continue = False
    for line in lines[1:]:
        lowered = line.lower()
        if lowered.startswith(b"content-length:"):
            length = int(line.split(b":", 1)[1])
        elif lowered.startswith(b"expect:") and b"100-continue" in lowered:
            expect_continue = True

    print(method.decode(), path.decode(), length)

    if method == b"GET" and path == b"/":
        respond(client, "200 OK", USAGE)
    elif method == b"GET" and path == b"/status":
        respond(
            client,
            "200 OK",
            '{"width":%d,"height":%d,"image_bytes":%d,"has_image":%s,"free_mem":%d}\n'
            % (WIDTH, HEIGHT, IMAGE_BYTES, "true" if has_image else "false", gc.mem_free()),
            "application/json",
        )
    elif method == b"GET" and path == b"/settings":
        respond(client, "200 OK", settings_json(), "application/json")
    elif method == b"POST" and path == b"/settings":
        error = apply_settings(query)
        if error:
            respond(client, "400 Bad Request", error + "\n")
        else:
            respond(client, "200 OK", settings_json(), "application/json")
    elif method == b"POST" and path == b"/clear":
        body[:] = bytes(IMAGE_BYTES)
        render()
        respond(client, "200 OK", "cleared\n")
    elif method == b"POST" and path == b"/image":
        if length != IMAGE_BYTES:
            respond(
                client,
                "400 Bad Request",
                "expected %d bytes (64x64 RGB888), Content-Length was %d\n" % (IMAGE_BYTES, length),
            )
            return
        if expect_continue:
            client.send(b"HTTP/1.1 100 Continue\r\n\r\n")
        view = memoryview(body)
        got = min(len(rest), IMAGE_BYTES)
        view[:got] = rest[:got]
        while got < IMAGE_BYTES:
            got += client.recv_into(view[got:], IMAGE_BYTES - got)
        render()
        respond(client, "200 OK", "ok\n")
    else:
        respond(client, "404 Not Found", USAGE)


def connect(esp, ssid, password):
    show_status("WiFi...")
    while not esp.is_connected:
        watchdog.feed()
        try:
            esp.connect_AP(ssid, password)
        except (RuntimeError, OSError) as e:
            print("WiFi connect failed:", e)
            show_status("WiFi\nfailed")
            time.sleep(5)
            show_status("WiFi...")
    ip = esp.pretty_ip(esp.ip_address)
    print("Connected, http://" + ip + "/")
    parts = ip.split(".")
    show_status("IP\n" + ".".join(parts[:2]) + ".\n" + ".".join(parts[2:]))


def listen(pool):
    sock = pool.socket(pool.AF_INET, pool.SOCK_STREAM)
    sock.bind(("", PORT))
    sock.listen(1)
    return sock


def main():
    esp = adafruit_esp32spi.ESP_SPIcontrol(
        busio.SPI(board.SCK, board.MOSI, board.MISO),
        DigitalInOut(board.ESP_CS),
        DigitalInOut(board.ESP_BUSY),
        DigitalInOut(board.ESP_RESET),
    )
    ssid = os.getenv("CIRCUITPY_WIFI_SSID")
    password = os.getenv("CIRCUITPY_WIFI_PASSWORD")
    if not ssid or not password:
        show_status("Set WiFi\nin\nsettings\n.toml")
        print("Set CIRCUITPY_WIFI_SSID and CIRCUITPY_WIFI_PASSWORD in settings.toml")
        while True:
            time.sleep(1)

    watchdog.timeout = WATCHDOG_TIMEOUT
    watchdog.mode = WatchDogMode.RESET

    connect(esp, ssid, password)
    pool = SocketPool(esp)
    server = listen(pool)
    last_check = time.monotonic()

    while True:
        watchdog.feed()
        now = time.monotonic()
        if now - last_check > 10:
            last_check = now
            if not esp.is_connected:
                connect(esp, ssid, password)
                server = listen(pool)
        try:
            client, _ = server.accept()
        except OSError:
            time.sleep(0.02)
            continue
        # the ESP32 hands the listening socket itself to the client, so a new listener is needed
        if client._socknum == server._socknum:
            server = listen(pool)
        try:
            handle(client)
        except Exception as e:
            print("request failed:", repr(e))
        client.close()
        gc.collect()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # Unhandled crash anywhere above: log it if a serial console is
        # attached, then hard-reset rather than leaving the board frozen
        # with a dead display until someone notices and power-cycles it.
        print("FATAL:", repr(e))
        time.sleep(2)  # give a connected serial console a moment to show the traceback
        microcontroller.reset()
