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
from PIL import Image
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
	return Image.open(io.BytesIO(resp.content)).convert('RGB')


def to_panel_bytes(img):
	"""Rotate for the physical stand orientation, letterbox onto a black
	64x64 canvas, and return raw RGB888 bytes."""
	fitted = img.transpose(Image.ROTATE_90)  # 90 degrees counter-clockwise
	fitted.thumbnail((PANEL_SIZE, PANEL_SIZE), Image.LANCZOS)
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
	state = {'shown': None}

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
		gets a grace period (KEEP) instead of falling through to some
		unrelated zone that's merely sitting paused on old content -
		that unrelated-paused-zone fallback only applies at startup,
		before anything has ever been shown.
		"""
		zones = candidate_zones()
		playing = [z for z in zones if z.get('state') == 'playing' and (z.get('now_playing') or {}).get('image_key')]
		if playing:
			if state['shown']:
				for z in playing:
					if z['zone_id'] == state['shown'][0]:
						return z
			return playing[0]

		if state['shown']:
			last_zone = api.zones.get(state['shown'][0])
			if last_zone and last_zone.get('state') in ('loading', 'paused'):
				return KEEP
			return None  # the zone we were tracking actually stopped

		paused = [z for z in zones if z.get('state') == 'paused' and (z.get('now_playing') or {}).get('image_key')]
		return paused[0] if paused else None

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
