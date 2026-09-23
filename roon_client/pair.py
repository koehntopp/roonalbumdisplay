#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = ["roonapi"]
# ///
"""
One-time pairing with a Roon Core on the local network.

Run this once. It discovers your Roon Core via LAN broadcast, registers
this script as a Roon Extension, and waits for you to approve it in
Roon > Settings > Extensions. On success it saves a core id and auth
token next to this file, which roon_to_matrix.py reuses on every run
so you don't have to re-approve.
"""

import time
from pathlib import Path

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


def main():
	print('Discovering Roon cores on the LAN...')
	discover = RoonDiscovery(None)
	servers = discover.all()
	discover.stop()

	if not servers:
		print('No Roon core found. Is Roon Core running and on the same network/subnet?')
		return

	print(f'Found {len(servers)} server(s): {servers}')
	apis = [RoonApi(APPINFO, None, host, port, False) for host, port in servers]

	print()
	print("Now open Roon, go to Settings > Extensions, and click 'Enable' on")
	print("'Matrix Portal Now Playing'.")
	print('Waiting for authorization (Ctrl-C to give up)...')

	authed = []
	try:
		while not authed:
			time.sleep(1)
			authed = [a for a in apis if a.token]
	except KeyboardInterrupt:
		print('\nGave up waiting.')
		for a in apis:
			a.stop()
		return

	api = authed[0]
	print(f'Authorized against core: {api.core_name} ({api.core_id})')

	CORE_ID_FILE.write_text(api.core_id)
	TOKEN_FILE.write_text(api.token)
	print(f'Saved {CORE_ID_FILE.name} and {TOKEN_FILE.name}.')
	print('You can now run roon_to_matrix.py.')

	for a in apis:
		a.stop()


if __name__ == '__main__':
	main()
