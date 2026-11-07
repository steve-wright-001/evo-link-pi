# Evo Link Pi station

A Raspberry Pi that does the Wi-Fi work for the **Evo Link** app: it joins each Shelly's hotspot itself, so your phone doesn't
have to keep agreeing to Wi-Fi connections, sets the hotspot password, runs the on/off test and sends the label to the Zebra
printer. You still drive everything from the app, one Shelly at a time. (The app has these same steps under Settings → Pi
station → How to set up a Pi.)

## What you need
- A Raspberry Pi 4 or 5 with **Raspberry Pi OS** (the 2023 "Bookworm" version or newer; Lite is fine).
- A **network cable from the Pi to your router**. The cable keeps the Pi on your network while its Wi-Fi visits each Shelly,
  so don't set up Wi-Fi on the Pi.

## Install (once)
0. **Get onto the Pi:** put Raspberry Pi OS on its SD card with **Raspberry Pi Imager**. In Imager's settings, set the name to
   `evolink`, pick a username and password, turn on **SSH**, and leave Wi-Fi blank. Start the Pi with its cable in, then from
   a computer (or an SSH app such as Termius on the phone):
   ```
   ssh yourname@evolink.local
   ```
   and type your password. No SSH? Plug a keyboard and screen into the Pi instead.
1. **Turn on the Pi's Wi-Fi:** `sudo raspi-config` → Localisation Options → WLAN Country → your country.
2. **Install:**
   ```
   curl -fsSL https://raw.githubusercontent.com/steve-wright-001/evo-link-pi/main/install.sh | sudo bash
   ```
   It sets the program to start whenever the Pi turns on, and shows the Pi's address.
3. **In the app:** Settings → Pi station → Find the Pi (or type the address), then Pair. Only that phone can use it after that.

## Update
Run the same install line again.

## If something's wrong
- **See what it's doing:** `journalctl -u evolink-pi -f`
- **Pair a different phone:** `sudo python3 /opt/evolink-pi/evolink-pi.py reset-pairing`, then Pair in the app.
- **Is it running?** `systemctl status evolink-pi` (it answers on port 8710).

## How it works
`evolink-pi.py` (Python 3, nothing else to install) serves a small JSON API on port 8710: `hello` and `pair` without a key,
then `scan`, `connect`, `disconnect`, `rpc`, `send`, `signal` and `cancel` with the key the app was given when it paired
(header `X-Evolink-Key`; the first app to pair is the only one). Joining a hotspot uses its own NetworkManager connection,
`evolink-shelly` (no default route, never connects by itself, deleted on leaving; the password goes in through a file only
root can read). The hotspot password comes from the app with each join and is never kept on the Pi. `--fake` runs a pretend
Pi with made-up Shellys and printer, for testing the app.
