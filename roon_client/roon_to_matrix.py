#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = ["roonapi", "requests", "Pillow"]
# ///
"""
Watches a Roon zone's now-playing state and pushes the current album art
to the Matrix Portal M4's HTTP image server (see ../device/code.py).

Run pair.py once first to authorize this script against your Roon Core.

Usage:
    uv run roon_to_matrix.py --panel-url http://192.168.1.172 [--zone "Living Room"]

If --zone is omitted, whichever zone is actually playing is shown
(falling back to a paused zone with art), re-evaluated on every Roon
state change rather than fixed at startup.
"""

import argparse
import io
import sys
import time
from pathlib import Path

import requests
from PIL import Image, ImageFilter, ImageOps
from roonapi import RoonApi, RoonDiscovery

APPINFO = {
	'extension_id': 'com.roonalbumdisplay.client',
	'display_name': 'Matrix Portal Now Playing',
	'display_version': '1.0.0',
	'publisher': 'roonalbumdisplay',
	'email': 'noreply@example.com',
}

STATE_DIR = Path(__file__).parent
CORE_ID_FILE = STATE_DIR / 'roon_core_id.txt'
TOKEN_FILE = STATE_DIR / 'roon_token.txt'

PANEL_SIZE = 64
POLL_IDLE_SECONDS = 1


def load_credentials():
	if not CORE_ID_FILE.exists() or not TOKEN_FILE.exists():
		sys.exit('Not paired yet. Run pair.py first.')
	return CORE_ID_FILE.read_text().strip(), TOKEN_FILE.read_text().strip()


def connect(core_id):
	print('Locating Roon core on the LAN...')
	discover = RoonDiscovery(core_id)
	server = discover.first()
	discover.stop()
	if not server:
		sys.exit(f'Could not find Roon core {core_id} on the LAN.')
	host, port = server
	token = TOKEN_FILE.read_text().strip()
	print(f'Connecting to {host}:{port}...')
	return RoonApi(APPINFO, token, host, port, True)


def fetch_art(api, image_key):
	url = api.get_image(image_key, scale='fit', width=PANEL_SIZE, height=PANEL_SIZE)
	resp = requests.get(url, timeout=5)
	resp.raise_for_status()
	img = Image.open(io.BytesIO(resp.content)).convert('RGB')
	# The panel can only show a handful of distinct levels per channel
	# (RGB565 truncation, on top of an already-dim brightness setting).
	# Album art varies hugely in how much of the 0-255 range it actually
	# uses; stretching each image's own histogram to the full range before
	# any of that quantization happens makes much better use of the few
	# levels available, instead of wasting them on a narrow input range.
	# cutoff=1 clips 1% at each end so a single stray bright/dark pixel
	# (e.g. a small logo) doesn't defeat the stretch. preserve_tone=True is
	# essential here: the default stretches R, G, and B independently based
	# on each channel's own min/max, with no regard for their relationship -
	# a strong solid-color background (e.g. green album art) can end up with
	# each channel stretched completely differently, producing a wrong hue
	# or even crushing the background to black. preserve_tone computes one
	# shared tone curve instead, so color balance survives the stretch.
	return ImageOps.autocontrast(img, cutoff=1, preserve_tone=True)


# Approximate sRGB gamma (2.2) decode/encode tables, applied via PIL's fast
# per-pixel point() LUT. Resizing directly on gamma-encoded (display-referred)
# values is a well-known source of muddy midtones and dark edge halos on a
# strong downscale (~8x here) - decoding to linear-ish light before the
# resize and re-encoding after gives a noticeably cleaner result, especially
# on high-contrast images. This is an 8-bit approximation (some precision is
# lost in the round-trip), not a true float/linear pipeline, but costs
# nothing extra to add (pure PIL, no numpy) and is a clear improvement over
# not doing it at all.
_GAMMA = 2.2
# tripled: PIL's point() needs one 256-entry table per band for a 3-band
# (RGB) image when passed as a flat list, not a single shared 256-entry one.
_DECODE_LUT = [round(255 * (i / 255) ** _GAMMA) for i in range(256)] * 3
_ENCODE_LUT = [round(255 * (i / 255) ** (1 / _GAMMA)) for i in range(256)] * 3


def to_panel_bytes(img):
	"""Rotate for the physical stand orientation, resize in linear-ish
	light, letterbox onto a black 64x64 canvas, and return raw RGB888
	bytes."""
	fitted = img.transpose(Image.ROTATE_90)  # 90 degrees counter-clockwise
	fitted = fitted.point(_DECODE_LUT)
	fitted.thumbnail((PANEL_SIZE, PANEL_SIZE), Image.LANCZOS)
	fitted = fitted.point(_ENCODE_LUT)
	# LANCZOS is a good downscale filter, but shrinking full-res art ~8x
	# still softens edges; a mild unsharp mask at the target resolution
	# restores some perceived detail. Sharpen after resizing (and after
	# re-encoding back to display gamma), not before - sharpening at full
	# res would just get blurred away by the downscale.
	fitted = fitted.filter(ImageFilter.UnsharpMask(radius=1, percent=60, threshold=2))
	canvas = Image.new('RGB', (PANEL_SIZE, PANEL_SIZE), (0, 0, 0))
	x = (PANEL_SIZE - fitted.width) // 2
	y = (PANEL_SIZE - fitted.height) // 2
	canvas.paste(fitted, (x, y))
	return canvas.tobytes()


def push_image(panel_url, raw_rgb888):
	resp = requests.post(f'{panel_url}/image', data=raw_rgb888, timeout=5)
	resp.raise_for_status()


def clear_panel(panel_url):
	requests.post(f'{panel_url}/clear', timeout=5)


RETRY_SECONDS = 15


def run_once(args):
	"""One connect-and-watch session. Raises on any unexpected failure so
	main()'s outer loop can reconnect instead of the whole daemon dying."""
	panel_url = args.panel_url.rstrip('/')

	core_id, _ = load_credentials()
	api = connect(core_id)

	# (zone_id, image_key) currently on the panel, so a change of *either*
	# a different zone taking over playback, or the same zone's art
	# changing, triggers a redraw. None means the panel shows nothing.
	# 'shown' starts as UNKNOWN (distinct from None) so a fresh start with
	# nothing playing still actively clears the panel, rather than trusting
	# whatever was already physically on it (leftover from a previous run,
	# a manual test push, etc.) to already be blank.
	UNKNOWN = object()
	state = {'shown': UNKNOWN}

	def candidate_zones():
		if args.zone:
			zone = api.zone_by_name(args.zone)
			if not zone:
				names = [z['display_name'] for z in api.zones.values()]
				print(f"Zone '{args.zone}' not found yet. Known zones: {names}")
				return []
			return [zone]
		return list(api.zones.values())

	KEEP = object()  # sentinel: no zone change, leave the panel as-is

	def pick_active_zone():
		"""Re-evaluated on every event. Prefers a zone that's actually
		playing (sticking with whichever zone we're already showing, if
		it's among them, so simultaneous multi-room playback doesn't
		flap). Does not latch onto one zone_id otherwise, so playback
		moving between zones is picked up.

		Roon reports a zone as briefly "loading" between tracks rather
		than "playing" continuously, so a zone we're already tracking
		gets a grace period (KEEP) for that specific transient state
		only - not for "paused", which is a deliberate pause and should
		turn the screen off like any other "nothing playing" case.

		If nothing anywhere is playing, returns None - the panel turns
		off rather than showing a paused zone's possibly old/stale art.
		This intentionally does *not* fall back to a paused zone even at
		startup (an earlier version did); "nothing playing" should mean
		"screen off", full stop.
		"""
		zones = candidate_zones()
		shown = state['shown']
		shown_zone_id = shown[0] if shown not in (None, UNKNOWN) else None
		playing = [z for z in zones if z.get('state') == 'playing' and (z.get('now_playing') or {}).get('image_key')]
		if playing:
			if shown_zone_id:
				for z in playing:
					if z['zone_id'] == shown_zone_id:
						return z
			return playing[0]

		if shown_zone_id:
			last_zone = api.zones.get(shown_zone_id)
			if last_zone and last_zone.get('state') == 'loading':
				return KEEP

		return None

	def refresh():
		zone = pick_active_zone()
		if zone is KEEP:
			return
		if zone is None:
			if state['shown'] is not None:
				print('No zone playing, clearing panel')
				try:
					clear_panel(panel_url)
				except requests.RequestException as e:
					print(f'Failed to clear panel: {e!r}')
				state['shown'] = None
			return

		now_playing = zone.get('now_playing') or {}
		image_key = now_playing['image_key']
		shown_key = (zone['zone_id'], image_key)
		if shown_key == state['shown']:
			return

		line = now_playing.get('three_line', {})
		print(f"[{zone['display_name']}] {line.get('line1')} / {line.get('line2')} / {line.get('line3')}")

		try:
			img = fetch_art(api, image_key)
		except Exception as e:
			print(f'Failed to fetch art: {e!r}')
			return
		try:
			push_image(panel_url, to_panel_bytes(img))
		except Exception as e:
			print(f'Failed to push image to panel: {e!r}')
			return
		state['shown'] = shown_key

	def on_state_change(event, changed_ids):
		# Runs on roonapi's own callback thread. Guard it explicitly rather
		# than relying on roonapi's dispatch loop to swallow our bugs too -
		# an uncaught exception here should be visible, not silently kill
		# the thread that delivers all future zone updates.
		try:
			refresh()
		except Exception as e:
			print(f'Error handling state change: {e!r}')

	api.register_state_callback(on_state_change, event_filter=['zones_changed', 'zones_seek_changed'])

	print('Known zones at startup:', [(z['display_name'], z.get('state')) for z in api.zones.values()])
	refresh()

	print('Watching for now-playing changes. Ctrl-C to stop.')
	try:
		while True:
			time.sleep(POLL_IDLE_SECONDS)
	finally:
		api.stop()


def main():
	parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
	parser.add_argument('--panel-url', required=True, help='e.g. http://192.168.1.172')
	parser.add_argument('--zone', help='Roon zone display name to watch (default: whichever zone is playing)')
	args = parser.parse_args()

	while True:
		try:
			run_once(args)
		except KeyboardInterrupt:
			print('Stopping.')
			break
		except Exception as e:
			print(f'Unexpected error, reconnecting in {RETRY_SECONDS}s: {e!r}')
			time.sleep(RETRY_SECONDS)


if __name__ == '__main__':
	main()
