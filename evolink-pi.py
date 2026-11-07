#!/usr/bin/env python3
"""Evo Link's Pi station (Steven, 2026-11-07: "Could I program a raspberry pi to be the thing that connects to the shellys ...
to avoid having to accept WiFi each time? Then controlled from this app?").

The Pi does the radio work the phone does in the app: scan for Shelly hotspots, join one, talk to it, leave, and send labels
to the Zebra. Its Wi-Fi hops between Shelly hotspots through NetworkManager (no prompts); it stays on your network by its
Ethernet cable, where the app and the Zebra are. The app drives it, one Shelly at a time, over a small JSON API on port 8710.

  sudo python3 evolink-pi.py serve              the service (pi/install.sh sets it up to start with the Pi)
  sudo python3 evolink-pi.py reset-pairing      forget the paired app, so another can pair
  python3 evolink-pi.py serve --fake --state D  a pretend Pi with made-up Shellys and printer (for the app's tests)

Pairing: the first app to pair gets a key, and every other call needs it (header X-Evolink-Key). The hotspot password is sent
by the app with each join and never kept on the Pi. Python 3 standard library only.
"""
import argparse
import json
import os
import random
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = '0.1.0'
PORT = 8710
CON = 'evolink-shelly'   # the NetworkManager connection used for a Shelly's hotspot
SHELLY_IP = '192.168.33.1'


class Err(Exception):
    def __init__(self, msg, code='error'):
        super().__init__(msg)
        self.code = code


# ---------- the real radio: NetworkManager (nmcli), iw, sockets ----------
class Real:
    def __init__(self, iface):
        self.iface = iface
        self.proc = None        # a running nmcli, so cancel() can stop it
        self.sock = None        # a label being sent, so cancel() can stop it
        self.cancelled = False

    def _run(self, args, timeout=60, check=True):
        if self.cancelled:
            raise Err('Stopped', 'stopped')
        self.proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            out, err = self.proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            raise Err('Timed out', 'unavailable')
        finally:
            code = self.proc.returncode
            self.proc = None
        if self.cancelled:
            raise Err('Stopped', 'stopped')
        if check and code != 0:
            raise Err((err or out).strip() or 'failed', 'unavailable')
        return out

    def scan(self):
        out = self._run(['nmcli', '-t', '-e', 'yes', '-f', 'SSID,SIGNAL,SECURITY,FREQ', 'dev', 'wifi', 'list',
                         'ifname', self.iface, '--rescan', 'yes'], timeout=30)
        best = {}
        for line in out.splitlines():
            parts = re.split(r'(?<!\\):', line)
            if len(parts) < 4:
                continue
            ssid = parts[0].replace('\\:', ':').replace('\\\\', '\\')
            if not ssid:
                continue
            pct = int(parts[1] or 0)
            level = round(pct / 2 - 100)   # NetworkManager's signal % back to dBm (its own mapping)
            sec = parts[2] not in ('', '--')
            freq = int(re.sub(r'\D', '', parts[3]) or 0)
            if ssid not in best or level > best[ssid]['level']:
                best[ssid] = {'ssid': ssid, 'level': level, 'secure': sec, 'freq': freq}
        return {'networks': list(best.values()), 'fresh': True}

    def connect(self, ssid, password, timeout_ms):
        self.disconnect()
        add = ['nmcli', 'con', 'add', 'type', 'wifi', 'ifname', self.iface, 'con-name', CON, 'ssid', ssid,
               'connection.autoconnect', 'no', 'ipv4.never-default', 'yes', 'ipv6.method', 'disabled']
        if password:
            add += ['wifi-sec.key-mgmt', 'wpa-psk']
        self._run(add, timeout=15)
        wait = str(max(5, int(timeout_ms / 1000)))
        if password:
            # The password goes in a file only root can read, not on the command line.
            fd, path = tempfile.mkstemp()
            try:
                with os.fdopen(fd, 'w') as f:
                    f.write('802-11-wireless-security.psk:' + password + '\n')
                try:
                    self._run(['nmcli', '--wait', wait, 'con', 'up', CON, 'passwd-file', path], timeout=int(wait) + 10)
                except Err as e:
                    # Some NetworkManager versions want the password stored on the connection itself (it's deleted on leaving).
                    if e.code == 'stopped' or 'ecret' not in str(e):
                        raise
                    self._run(['nmcli', 'con', 'modify', CON, 'wifi-sec.psk', password], timeout=15)
                    self._run(['nmcli', '--wait', wait, 'con', 'up', CON], timeout=int(wait) + 10)
            finally:
                os.unlink(path)
        else:
            self._run(['nmcli', '--wait', wait, 'con', 'up', CON], timeout=int(wait) + 10)
        return {'ssid': ssid}

    def disconnect(self):
        subprocess.run(['nmcli', 'con', 'down', CON], capture_output=True)
        subprocess.run(['nmcli', 'con', 'delete', CON], capture_output=True)

    def rpc(self, method, params, timeout_ms):
        body = json.dumps({'id': 1, 'src': 'evolink-pi', 'method': method, 'params': params or {}}).encode()
        req = urllib.request.Request(f'http://{SHELLY_IP}/rpc', data=body, headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=timeout_ms / 1000) as r:
                text = r.read().decode() or '{}'
        except urllib.error.HTTPError as e:
            text = e.read().decode() or '{}'
        except Exception as e:
            raise Err(f'{type(e).__name__}: {e}', 'io')
        r = json.loads(text)
        if 'error' in r:
            raise Err((r['error'] or {}).get('message', 'Shelly error'), 'shelly')
        return {'result': r.get('result')}

    def send(self, host, port, data, timeout_ms):
        end = time.time() + max(5, timeout_ms / 1000)
        last = None
        while time.time() < end and not self.cancelled:
            try:
                s = socket.create_connection((host, port), timeout=5)
                self.sock = s
                s.sendall(data.encode('utf-8'))
                s.close()
                self.sock = None
                return {'bytes': len(data)}
            except Exception as e:
                last = e
                self.sock = None
                time.sleep(1.5)
        if self.cancelled:
            raise Err('Stopped', 'stopped')
        raise Err(f'Printer didn\'t answer at {host}:{port} ({last})', 'noprinter')

    def signal(self):
        out = subprocess.run(['iw', 'dev', self.iface, 'link'], capture_output=True, text=True).stdout
        m = re.search(r'signal:\s*(-?\d+)\s*dBm', out)
        return {'rssi': int(m.group(1)) if m else None}

    def cancel(self):
        self.cancelled = True
        p = self.proc
        if p:
            try:
                p.kill()
            except Exception:
                pass
        s = self.sock
        if s:
            try:
                s.close()
            except Exception:
                pass


# ---------- a pretend Pi, for the app's tests ----------
class Fake:
    def __init__(self):
        self.aps = {'Shelly1G3-E4B063FE6AE8': {'pass': '', 'level': -52},
                    'Shelly1PMG3-A0B1C2D3E4F5': {'pass': '', 'level': -63}}
        self.joined = None
        self.out = False
        self.timer = None
        self.printed = []
        self.cancelled = False
        self.slow = 0

    def scan(self):
        nets = [{'ssid': s, 'level': a['level'], 'secure': bool(a['pass']), 'freq': 2412} for s, a in self.aps.items()]
        return {'networks': nets + [{'ssid': 'Office', 'level': -45, 'secure': True, 'freq': 5180}], 'fresh': True}

    def connect(self, ssid, password, timeout_ms):
        time.sleep(0.2)
        a = self.aps.get(ssid)
        if not a or (a['pass'] or '') != (password or ''):
            raise Err('Couldn\'t join ' + ssid, 'unavailable')
        self.joined = ssid
        return {'ssid': ssid}

    def disconnect(self):
        self.joined = None

    def rpc(self, method, params, timeout_ms):
        if not self.joined:
            raise Err('Not joined to a Shelly', 'nolink')
        now = time.time()
        if self.timer and now >= self.timer[0] + self.timer[1]:
            self.out, self.timer = False, None
        mac = self.joined.split('-')[1]
        if method == 'Shelly.GetDeviceInfo':
            return {'result': {'id': 'shelly-' + mac.lower(), 'mac': mac, 'model': 'S3SW-001X16EU', 'gen': 3, 'ver': '1.4.4'}}
        if method == 'WiFi.SetConfig':
            self.aps[self.joined]['pass'] = params['config']['ap']['pass']
            self.joined = None
            raise Err('connection reset', 'io')     # the hotspot restarts before it answers, as a real one
        if method == 'Switch.Set':
            self.out = bool(params.get('on'))
            self.timer = (now, params['toggle_after']) if self.out and params.get('toggle_after') else None
            return {'result': {'was_on': not self.out}}
        sw = {'id': 0, 'output': self.out, 'temperature': {'tC': 40.5}}
        if self.timer:
            sw.update(timer_started_at=self.timer[0], timer_duration=self.timer[1])
        if method == 'Switch.GetStatus':
            return {'result': sw}
        if method == 'Shelly.GetStatus':
            return {'result': {'switch:0': sw, 'sys': {'unixtime': int(now)}}}
        raise Err('No method ' + method, 'shelly')

    def send(self, host, port, data, timeout_ms):
        end = time.time() + self.slow
        while time.time() < end:
            if self.cancelled:
                raise Err('Stopped', 'stopped')
            time.sleep(0.05)
        self.printed.append({'host': host, 'port': port, 'data': data})
        return {'bytes': len(data)}

    def signal(self):
        # The Pi's link to the Shelly it's joined to, wandering a little as a real one does.
        if not self.joined:
            return {'rssi': None}
        self.drift = max(-6, min(6, getattr(self, 'drift', 0) + random.choice((-1, 0, 1))))
        return {'rssi': self.aps[self.joined]['level'] + self.drift}

    def cancel(self):
        self.cancelled = True


# ---------- the API ----------
class Station:
    def __init__(self, radio, state_dir, fake):
        self.radio, self.state_dir, self.fake = radio, state_dir, fake
        self.lock = threading.Lock()           # one radio job at a time
        os.makedirs(state_dir, exist_ok=True)
        self.key_file = os.path.join(state_dir, 'key')

    def key(self):
        try:
            with open(self.key_file) as f:
                return f.read().strip() or None
        except FileNotFoundError:
            return None

    def pair(self):
        if self.key():
            raise Err('Already paired with an app. On the Pi: sudo python3 /opt/evolink-pi/evolink-pi.py reset-pairing', 'paired')
        k = secrets.token_urlsafe(24)
        fd = os.open(self.key_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as f:
            f.write(k)
        return {'key': k}

    def hello(self):
        return {'app': 'evolink-pi', 'version': VERSION, 'name': socket.gethostname(), 'paired': bool(self.key()), 'fake': self.fake}

    def call(self, op, b):
        r = self.radio
        if op == 'cancel':
            r.cancel()
            return {}
        if op == 'signal':
            return r.signal()
        with self.lock:
            r.cancelled = False
            if op == 'scan':
                return r.scan()
            if op == 'connect':
                return r.connect(b.get('ssid') or '', b.get('password') or '', b.get('timeoutMs', 30000))
            if op == 'disconnect':
                r.disconnect()
                return {}
            if op == 'rpc':
                return r.rpc(b.get('method'), b.get('params') or {}, b.get('timeoutMs', 8000))
            if op == 'send':
                return r.send(b.get('host'), int(b.get('port', 9100)), b.get('data', ''), b.get('timeoutMs', 30000))
        raise Err('Unknown call ' + op, 'bad')


def handler_for(station):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Headers', 'Content-Type, X-Evolink-Key')
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):
            self._send(204, {})

        def do_GET(self):
            if self.path == '/api/hello':
                return self._send(200, station.hello())
            if station.fake and self.path == '/api/fake/printed':
                return self._send(200, {'printed': station.radio.printed, 'aps': station.radio.aps})
            self._send(404, {'ok': False, 'error': 'Not found', 'code': 'bad'})

        def do_POST(self):
            n = int(self.headers.get('Content-Length') or 0)
            try:
                b = json.loads(self.rfile.read(n) or b'{}')
            except Exception:
                return self._send(400, {'ok': False, 'error': 'Bad JSON', 'code': 'bad'})
            op = self.path.rsplit('/', 1)[-1]
            try:
                if self.path == '/api/pair':
                    return self._send(200, dict(ok=True, **station.pair()))
                if station.fake and self.path == '/api/fake/set':
                    for k, v in b.items():
                        setattr(station.radio, k, v)
                    return self._send(200, {'ok': True})
                if not station.key() or self.headers.get('X-Evolink-Key') != station.key():
                    return self._send(401, {'ok': False, 'error': 'Not paired with this app', 'code': 'unpaired'})
                return self._send(200, dict(ok=True, **station.call(op, b)))
            except Err as e:
                return self._send(200, {'ok': False, 'error': str(e), 'code': e.code})
            except Exception as e:
                return self._send(200, {'ok': False, 'error': f'{type(e).__name__}: {e}', 'code': 'error'})
    return H


def main():
    ap = argparse.ArgumentParser(description='Evo Link Pi station')
    ap.add_argument('cmd', choices=['serve', 'reset-pairing'])
    ap.add_argument('--port', type=int, default=PORT)
    ap.add_argument('--iface', default='wlan0')
    ap.add_argument('--state', default='/var/lib/evolink-pi')
    ap.add_argument('--fake', action='store_true')
    a = ap.parse_args()
    if a.cmd == 'reset-pairing':
        try:
            os.unlink(os.path.join(a.state, 'key'))
        except FileNotFoundError:
            pass
        print('Pairing reset: the next app to pair becomes the Pi\'s app.')
        return
    station = Station(Fake() if a.fake else Real(a.iface), a.state, a.fake)
    srv = ThreadingHTTPServer(('0.0.0.0', a.port), handler_for(station))
    print(f'Evo Link Pi station {VERSION} on port {a.port}{" (pretend)" if a.fake else ""}', flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
