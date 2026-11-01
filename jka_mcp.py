#!/usr/bin/env python3
"""jka-mcp: an MCP server that runs Jedi Knight: Jedi Academy (an OpenJK based client such as VisionJK) without a
window and lets an AI assistant use it: console commands, screenshots, the mouse and keys in the menus, a dump of
the menus with the rectangles of their items, and reloading menus or modules into the running game.

One game at a time, started in gamescope's headless backend (no window, no sound). The server speaks the Model
Context Protocol over stdio (JSON-RPC 2.0, one message a line) and needs only the Python standard library. With
Pillow installed, screenshots can be made smaller before they are sent.

Configuration: a JSON file named by JKA_MCP_CONFIG (default: jka-mcp.json next to this file), keys see CONFIG below.
"""
import base64
import collections
import glob
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time

VERSION = '0.1.0'
PROTOCOLS = ('2025-11-25', '2025-06-18', '2025-03-26', '2024-11-05')

INSTRUCTIONS = (
    'Drives one Jedi Academy client (VisionJK) without a window. Start it with launch, end it with quit. '
    'Coordinates are pixels of the game window, the same as in a full size screenshot (menu_dump gives each item '
    'its rect x,y,w,h). Prefer click by item name over mouse; pass screenshot=true to an action to see the result '
    'in the same call. reload rebuilds menus or modules and loads them into the running game.')

# what the configuration file may say, and what it is without it
CONFIG = {
    'game_dir': '',                 # folder with the executable of the game (and base/ with the assets)
    'binary': './visionjk.x86_64',  # the executable, relative to game_dir
    'home': '',                     # fs_homepath for the runs (configs, screenshots); empty: <game_dir>/../jka-mcp-home
    'fs_game': 'japlus',            # the mod folder the client ends up in (where screenshots/ is)
    'launcher': 'gamescope',        # 'gamescope' (headless, no window) or 'direct' (a window on the desktop)
    'env': {},                      # more environment, e.g. {"VK_DRIVER_FILES": "/usr/share/vulkan/icd.d/radeon_icd.json"}
    # (com_bootlogo 0: with only +set words the engine plays the opening logos, which never end without a window)
    'args': ['+set', 'com_bootlogo', '0', '+set', 'v_updateCheck', '0', '+set', 'cl_motd', '0', '+set', 's_volume', '0', '+set', 's_musicvolume', '0',
             '+set', 'com_skipIntro', '1'],
    'renderer': 'rd-vulkan-vision', # cl_renderer, empty: the game's own choice
    'reload': {                     # what reload runs; each a list of commands (argv lists), run in 'cwd'
        'cwd': '',
        'menus': [],                # e.g. [["python3", "tools/menus/build.py"]]
        'menus_copy': [],           # [source dir, destination under home/<fs_game>]: loose files the game reads first
        'modules': [],              # e.g. [["ninja", "-C", "build64", "uix86_64.so", "cgamex86_64.so"]]
        'modules_copy': [],         # [[built file, destination relative to game_dir], ...]
    },
    'log_file': '',                 # where the output of the game goes as well; empty: <home>/jka-mcp.log
}

# key numbers of the game (codemp/ui/keycodes.h) for the names the tool 'key' takes
KEYS = {'shift': 1, 'ctrl': 2, 'alt': 3, 'backspace': 8, 'tab': 9, 'enter': 10, 'escape': 27, 'esc': 27, 'f1': 28,
        'space': 32, 'delete': 127, 'mwheelup': 137, 'mwheeldown': 139, 'mouse1': 141, 'mouse2': 142, 'home': 144,
        'pageup': 145, 'end': 157, 'pagedown': 158, 'up': 170, 'down': 171, 'left': 172, 'right': 173}


def load_config():
    path = os.environ.get('JKA_MCP_CONFIG') or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'jka-mcp.json')
    cfg = json.loads(json.dumps(CONFIG))
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            user = json.load(f)
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    if not cfg['home'] and cfg['game_dir']:
        cfg['home'] = os.path.join(os.path.dirname(os.path.abspath(cfg['game_dir'])), 'jka-mcp-home')
    if not cfg['log_file'] and cfg['home']:
        cfg['log_file'] = os.path.join(cfg['home'], 'jka-mcp.log')
    return cfg


class Game:
    """The running game: its process, its output (a queue of lines a reader thread fills) and its stdin, which the
    console of the client reads line by line when it is not a terminal."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.proc = None
        self.lines = collections.deque(maxlen=20000)
        self.count = 0              # lines seen so far, for 'since'
        self.cond = threading.Condition()
        self.shot = 0
        self.size = (0, 0)
        self.log = None

    # ---- the process

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, width=1920, height=1080, extra=None):
        if self.running():
            raise RuntimeError('the game is already running; quit it first')
        cfg = self.cfg
        game_dir = os.path.abspath(cfg['game_dir'])
        if not os.path.isdir(game_dir):
            raise RuntimeError('game_dir is not set or not a folder: %r' % cfg['game_dir'])
        home = os.path.abspath(cfg['home'])
        # (an OpenJK client may refuse game code from a temporary folder: keep home and game out of /tmp)
        os.makedirs(os.path.join(home, cfg['fs_game']), exist_ok=True)
        args = [cfg['binary'], '+set', 'fs_homepath', home, '+set', 'r_mode', '-1', '+set', 'r_customwidth', str(width),
                '+set', 'r_customheight', str(height), '+set', 'r_fullscreen', '0']
        if cfg.get('renderer'):
            args += ['+set', 'cl_renderer', cfg['renderer']]
        args += list(cfg.get('args') or []) + list(extra or [])
        env = dict(os.environ)
        env.update({k: str(v) for k, v in (cfg.get('env') or {}).items()})
        env['SDL_AUDIODRIVER'] = env.get('SDL_AUDIODRIVER', 'dummy')
        if cfg['launcher'] == 'gamescope':
            for k in ('DISPLAY', 'WAYLAND_DISPLAY'):
                env.pop(k, None)
            env['SDL_VIDEODRIVER'] = 'x11'
            cmd = ['gamescope', '--backend', 'headless', '-W', str(width), '-H', str(height), '-w', str(width),
                   '-h', str(height), '--'] + args
        else:
            cmd = args
        self.log = open(cfg['log_file'], 'a', encoding='utf-8', errors='replace') if cfg.get('log_file') else None
        self.lines.clear()
        self.proc = subprocess.Popen(cmd, cwd=game_dir, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, start_new_session=True)
        self.size = (width, height)
        threading.Thread(target=self._read, daemon=True).start()
        # ready when the engine has run its startup and the first frame of the menus is near
        if not self.wait_for(r'Common Initialization Complete|execing visionjk\.cfg|execing autoexec', 60):
            out = self.tail(40)[0]
            self.stop()
            raise RuntimeError('the game did not start:\n' + out)
        # then for the first menu (a +map or +connect on the command line may keep the menus closed)
        menus = []
        end = time.time() + 20
        while time.time() < end and self.running():
            try:
                menus = [m['name'] for m in self.dump()['menus']]
            except RuntimeError:
                break
            if menus:
                break
            time.sleep(0.5)
        return 'started %dx%d (pid %d), open menus: %s' % (width, height, self.proc.pid, ', '.join(menus) or 'none')

    def stop(self):
        if not self.proc:
            return 'not running'
        if self.proc.poll() is None:
            try:
                self.send('quit')
                self.proc.wait(timeout=8)
            except Exception:
                pass
        if self.proc.poll() is None or self.cfg['launcher'] == 'gamescope':
            # gamescope may stay with its helpers: end the whole session of the run
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except Exception:
                pass
            try:
                self.proc.wait(timeout=5)
            except Exception:
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except Exception:
                    pass
        code = self.proc.returncode
        self.proc = None
        if self.log:
            self.log.close()
            self.log = None
        return 'stopped (exit code %s)' % code

    def _read(self):
        proc = self.proc
        for raw in iter(proc.stdout.readline, b''):
            line = raw.decode('utf-8', 'replace').rstrip('\r\n')
            # (the colour codes of the game: ^ and a letter or digit)
            line = re.sub(r'\^[0-9a-zA-Z]', '', line)
            # (VisionJK puts the date and time in front of every line)
            line = re.sub(r'^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d ', '', line)
            with self.cond:
                self.lines.append(line)
                self.count += 1
                self.cond.notify_all()
            if self.log:
                self.log.write(line + '\n')
                self.log.flush()
        with self.cond:
            self.cond.notify_all()

    # ---- the console

    def send(self, command):
        if not self.running():
            raise RuntimeError('the game is not running; use launch first')
        for line in command.splitlines():
            self.proc.stdin.write((line + '\n').encode('utf-8'))
        self.proc.stdin.flush()

    def mark(self):
        with self.cond:
            return self.count

    def since(self, mark):
        with self.cond:
            n = self.count - mark
            return list(self.lines)[-n:] if n > 0 else []

    def wait_for(self, pattern, timeout, mark=None):
        """Waits until a line after mark matches pattern; returns that line or None."""
        rx = re.compile(pattern)
        start = self.mark() if mark is None else mark
        end = time.time() + timeout
        with self.cond:
            while True:
                n = self.count - start
                for line in (list(self.lines)[-n:] if n > 0 else []):
                    if rx.search(line):
                        return line
                if self.proc is None or self.proc.poll() is not None:
                    return None
                left = end - time.time()
                if left <= 0:
                    return None
                self.cond.wait(min(left, 0.25))

    def command(self, command, wait=0.5, until=None, timeout=10.0):
        """Sends a command and returns what the game printed after it: until a line matches 'until', else for 'wait'
        seconds."""
        m = self.mark()
        self.send(command)
        if until:
            self.wait_for(until, timeout, m)
        else:
            time.sleep(wait)
        return self.since(m)

    def tail(self, n=50, since=None):
        with self.cond:
            lines = list(self.lines)
            if since is not None:
                lines = lines[-(self.count - since):] if self.count > since else []
            return '\n'.join(lines[-n:]), self.count

    # ---- what the tools build on

    def screenshot(self, timeout=15.0):
        self.shot += 1
        name = 'jkamcp_%d_%d' % (os.getpid(), self.shot)
        m = self.mark()
        self.send('screenshot_png %s' % name)
        line = self.wait_for(r'Wrote screenshots/%s\.png' % name, timeout, m)
        if not line:
            raise RuntimeError('no screenshot was written:\n' + '\n'.join(self.since(m)[-15:]))
        home = os.path.abspath(self.cfg['home'])
        found = glob.glob(os.path.join(home, '*', 'screenshots', name + '.png'))
        if not found:
            found = glob.glob(os.path.join(os.path.abspath(self.cfg['game_dir']), '*', 'screenshots', name + '.png'))
        if not found:
            raise RuntimeError('the game wrote %s.png, but it is not under %s' % (name, home))
        with open(found[0], 'rb') as f:
            data = f.read()
        os.remove(found[0])
        return data

    def dump(self):
        """The menus as the ui sees them (VisionJK: v_uiDump)."""
        lines = self.command('v_uiDump', until=r'^uidump end', timeout=5)
        out = {'screen': None, 'menus': []}
        for line in lines:
            if not line.startswith('uidump '):
                continue
            parts = line.split(' ')
            if parts[1] == 'screen':
                v = parts[2:]
                out['screen'] = {'width': int(v[0]), 'height': int(v[1]), 'xscale': float(v[2]), 'yscale': float(v[3]),
                                 'bias': float(v[4]), 'biasY': float(v[5])}
                # (the ui keeps the pointer in its 640x480 units: in window pixels like everything else here)
                sc = out['screen']
                sc['cursor'] = [round((int(v[6]) + sc['bias']) * sc['xscale']), round((int(v[7]) + sc['biasY']) * sc['yscale'])]
            elif parts[1] == 'menu':
                menu = {'name': parts[2], 'items': []}
                for kv in parts[3:]:
                    k, _, val = kv.partition('=')
                    menu[k] = val
                out['menus'].append(menu)
            elif parts[1] == 'item' and out['menus']:
                rest = line.split(' ', 5)
                item = {'index': int(rest[3]), 'name': rest[4]}
                tail = rest[5]
                # type=, px=, flags=, cvar=, value= are single words; text= is the rest of the line
                text_at = tail.find(' text=')
                head, item['text'] = (tail[:text_at], tail[text_at + 6:]) if text_at >= 0 else (tail, '')
                for kv in head.split(' '):
                    k, _, val = kv.partition('=')
                    if k == 'px':
                        item['rect'] = [float(a) for a in val.split(',')]
                    elif k == 'cvar' and val in ('', '-'):
                        continue
                    elif k:
                        item[k] = val
                out['menus'][-1]['items'].append(item)
        if out['screen'] is None:
            raise RuntimeError('no answer to v_uiDump (it needs a ui of VisionJK): ' + ' | '.join(lines[-5:]))
        return out


# ---- the tools

def image_content(data, max_width, quality):
    """A screenshot as MCP image content: smaller and as JPEG where Pillow is there, else the PNG as it is."""
    try:
        from PIL import Image
        import io
        im = Image.open(io.BytesIO(data)).convert('RGB')
        if max_width and im.width > max_width:
            im = im.resize((max_width, round(im.height * max_width / im.width)), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, 'JPEG', quality=quality)
        return {'type': 'image', 'data': base64.b64encode(buf.getvalue()).decode(), 'mimeType': 'image/jpeg'}
    except ImportError:
        return {'type': 'image', 'data': base64.b64encode(data).decode(), 'mimeType': 'image/png'}


def text(s):
    return {'type': 'text', 'text': s}


def find_item(game, query, menu_name=None):
    """An item of the open menus by its name or by words of its text (the first one that can be clicked)."""
    q = query.lower()
    best = None
    for menu in reversed(game.dump()['menus']):
        if menu_name and menu['name'].lower() != menu_name.lower():
            continue
        for item in menu['items']:
            if 'D' in item.get('flags', '') or 'rect' not in item:
                continue
            if item['name'].lower() == q or item['text'].lower() == q:
                return menu, item
            if best is None and q in item['text'].lower():
                best = (menu, item)
    if best:
        return best
    raise RuntimeError('no item named or saying %r in the open menus' % query)


def schema(props=None, required=None):
    out = {'type': 'object', 'properties': props or {}}
    if required:
        out['required'] = required
    return out


SHOT = {'screenshot': {'type': 'boolean', 'default': False, 'description': 'also return a screenshot taken after the action'}}
RO = {'readOnlyHint': True, 'openWorldHint': False}
ACT = {'readOnlyHint': False, 'destructiveHint': False, 'openWorldHint': False}

TOOLS = [
    {'name': 'launch', 'title': 'Start the game',
     'description': 'Start the game without a window (gamescope headless, no sound) and wait for the first menu. Call this first; '
                    'one game at a time. width/height: window size in pixels. extra: more command line words, e.g. ["+devmap", "mp/ffa1"].',
     'inputSchema': schema({'width': {'type': 'integer', 'default': 1920}, 'height': {'type': 'integer', 'default': 1080},
                            'extra': {'type': 'array', 'items': {'type': 'string'}}}),
     'annotations': {'readOnlyHint': False, 'destructiveHint': False, 'idempotentHint': False, 'openWorldHint': False}},
    {'name': 'quit', 'title': 'End the game', 'description': 'End the running game (quit, then end its process group).',
     'inputSchema': schema(), 'annotations': {'readOnlyHint': False, 'destructiveHint': True, 'idempotentHint': True, 'openWorldHint': False}},
    {'name': 'console', 'title': 'Console command',
     'description': 'Run console commands (one a line) and return what the game printed after them. until: a regular expression; '
                    'returns as soon as a printed line matches it (or after timeout seconds). Without until it waits wait seconds.',
     'inputSchema': schema({'command': {'type': 'string'}, 'wait': {'type': 'number', 'default': 0.5},
                            'until': {'type': 'string'}, 'timeout': {'type': 'number', 'default': 10}}, ['command']),
     'annotations': ACT},
    {'name': 'screenshot', 'title': 'Screenshot',
     'description': 'Picture of the game window now, as JPEG. max_width shrinks it (0: full size); pixel coordinates of the other '
                    'tools always refer to the full window size.',
     'inputSchema': schema({'max_width': {'type': 'integer', 'default': 1280}, 'quality': {'type': 'integer', 'default': 85}}),
     'annotations': RO},
    {'name': 'menu_dump', 'title': 'List menu items',
     'description': 'The open menus and their visible items: name, type, rect x,y,w,h in window pixels, flags (F focus, M mouse over, '
                    'D decoration, X disabled), cvar=value and text. filter: only items whose name, text or cvar contains it. '
                    'format: "concise" (one line per item, default) or "json" (also as structuredContent).',
     'inputSchema': schema({'filter': {'type': 'string'}, 'format': {'type': 'string', 'enum': ['concise', 'json'], 'default': 'concise'},
                            'decorations': {'type': 'boolean', 'default': False, 'description': 'include items flagged D'}}),
     'annotations': RO},
    {'name': 'mouse', 'title': 'Mouse',
     'description': 'Move the menu pointer to x, y (window pixels) and optionally click, hold, release, wheelup or wheeldown there.',
     'inputSchema': schema(dict({'x': {'type': 'number'}, 'y': {'type': 'number'},
                                 'action': {'type': 'string', 'enum': ['move', 'click', 'hold', 'release', 'wheelup', 'wheeldown'], 'default': 'move'}},
                                **SHOT), ['x', 'y']),
     'annotations': ACT},
    {'name': 'click', 'title': 'Click item',
     'description': 'Click the middle of a menu item found by its exact name or text, else the first item whose text contains the words '
                    '(topmost menu first). menu: limit the search to one menu.',
     'inputSchema': schema(dict({'item': {'type': 'string'}, 'menu': {'type': 'string'}}, **SHOT), ['item']),
     'annotations': ACT},
    {'name': 'key', 'title': 'Key or text',
     'description': 'Press a key in the menus, key: one character or one of ' + ', '.join(sorted(KEYS)) + '. text: type a string '
                    'into the focused field. Both may be given (key first).',
     'inputSchema': schema(dict({'key': {'type': 'string'}, 'text': {'type': 'string'}}, **SHOT)),
     'annotations': ACT},
    {'name': 'reload', 'title': 'Rebuild and reload',
     'description': 'Rebuild from source and load into the running game. menus: menu build, copy the menu files where the game reads '
                    'them first, ui_load. modules: module build, put the libraries in place (copy and rename), vid_restart.',
     'inputSchema': schema({'what': {'type': 'string', 'enum': ['menus', 'modules']}}, ['what']),
     'annotations': {'readOnlyHint': False, 'destructiveHint': False, 'idempotentHint': True, 'openWorldHint': False}},
    {'name': 'log', 'title': 'Game output',
     'description': 'Lines the game printed: the last "lines", or with since (the cursor a previous call returned) only the newer ones.',
     'inputSchema': schema({'lines': {'type': 'integer', 'default': 60}, 'since': {'type': 'integer'}}),
     'annotations': RO},
]


def run_commands(cfg, commands):
    out = []
    cwd = cfg['reload'].get('cwd') or None
    for argv in commands:
        r = subprocess.run(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=900)
        out.append('$ %s (exit %d)\n%s' % (' '.join(argv), r.returncode, r.stdout[-3000:]))
        if r.returncode != 0:
            raise RuntimeError('\n'.join(out))
    return '\n'.join(out)


def put_file(src, dst):
    """A file into place without writing into the one a running game has open: a copy next to it, then a rename."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = dst + '.new'
    shutil.copy2(src, tmp)
    os.replace(tmp, dst)


def concise(d, flt, decorations):
    sc = d['screen']
    out = ['screen %dx%d cursor %d,%d' % (sc['width'], sc['height'], sc['cursor'][0], sc['cursor'][1])]
    for menu in d['menus']:
        out.append('menu %s focus=%s page=%s' % (menu['name'], menu.get('focus', ''), menu.get('page', '')))
        for it in menu['items']:
            if not decorations and 'D' in it.get('flags', ''):
                continue
            if flt and not any(flt in (it.get(k) or '').lower() for k in ('name', 'text', 'cvar')):
                continue
            r = ','.join('%d' % v for v in it.get('rect', []))
            cv = ' %s=%s' % (it['cvar'], it.get('value', '')) if it.get('cvar') else ''
            fl = ' [%s]' % it['flags'] if it.get('flags') else ''
            out.append('  %s %s %s%s%s %s' % (it['name'], it.get('type', ''), r, fl, cv, json.dumps(it['text'])))
    return '\n'.join(out)


def after(game, a, msg):
    out = [text(msg)]
    if a.get('screenshot'):
        time.sleep(0.25)    # (a few frames for the menu to answer)
        out.append(image_content(game.screenshot(), 1280, 85))
    return out


def call(game, cfg, name, a):
    """Returns (content, structuredContent or None)."""
    if name == 'launch':
        return [text(game.start(int(a.get('width', 1920)), int(a.get('height', 1080)), a.get('extra')))], None
    if name == 'quit':
        return [text(game.stop())], None
    if name == 'console':
        lines = game.command(a['command'], float(a.get('wait', 0.5)), a.get('until'), float(a.get('timeout', 10)))
        return [text('\n'.join(lines) or '(nothing printed)')], None
    if name == 'screenshot':
        return [image_content(game.screenshot(), int(a.get('max_width', 1280)), int(a.get('quality', 85)))], None
    if name == 'menu_dump':
        d = game.dump()
        flt = (a.get('filter') or '').lower()
        if a.get('format') == 'json':
            for menu in d['menus']:
                menu['items'] = [i for i in menu['items']
                                 if (a.get('decorations') or 'D' not in i.get('flags', ''))
                                 and (not flt or any(flt in (i.get(k) or '').lower() for k in ('name', 'text', 'cvar')))]
            return [text(json.dumps(d))], d
        return [text(concise(d, flt, a.get('decorations')))], None
    if name == 'mouse':
        action = '' if a.get('action') in (None, '', 'move') else a['action']
        game.send('v_uiMouse %s %s %s px' % (a['x'], a['y'], action))
        time.sleep(0.15)
        return after(game, a, 'pointer at %s,%s %s' % (a['x'], a['y'], action or 'moved')), None
    if name == 'click':
        menu, item = find_item(game, a['item'], a.get('menu'))
        x, y, w, h = item['rect']
        game.send('v_uiMouse %d %d click px' % (round(x + w / 2), round(y + h / 2)))
        time.sleep(0.2)
        return after(game, a, 'clicked %s %s of %s at %d,%d' % (item['name'], json.dumps(item['text']), menu['name'],
                                                                 x + w / 2, y + h / 2)), None
    if name == 'key':
        sent = []
        if a.get('key'):
            k = a['key']
            num = KEYS.get(k.lower())
            if num is None and len(k) == 1:
                num = ord(k)
            if num is None:
                raise RuntimeError('unknown key %r; use one character or one of %s' % (k, ', '.join(sorted(KEYS))))
            game.send('v_uiKey %d' % num)
            sent.append(k)
        if a.get('text'):
            game.send('\n'.join('v_uiKey %d char' % ord(ch) for ch in a['text']))
            sent.append(json.dumps(a['text']))
        time.sleep(0.15)
        return after(game, a, 'pressed ' + ' '.join(sent)), None
    if name == 'reload':
        r = cfg['reload']
        out = []
        if a['what'] == 'menus':
            out.append(run_commands(cfg, r.get('menus') or []))
            if r.get('menus_copy'):
                src, dst = r['menus_copy']
                dst = os.path.join(os.path.abspath(cfg['home']), cfg['fs_game'], dst)
                n = 0
                for root, _, files in os.walk(src):
                    for f in files:
                        rel = os.path.relpath(os.path.join(root, f), src)
                        put_file(os.path.join(root, f), os.path.join(dst, rel))
                        n += 1
                out.append('copied %d files to %s' % (n, dst))
            if game.running():
                out.append('\n'.join(game.command('ui_load', 2.0)[-20:]))
        else:
            out.append(run_commands(cfg, r.get('modules') or []))
            for src, dst in r.get('modules_copy') or []:
                put_file(src, os.path.join(os.path.abspath(cfg['game_dir']), dst))
                out.append('put %s' % dst)
            if game.running():
                out.append('\n'.join(game.command('vid_restart', 4.0)[-20:]))
        return [text('\n'.join(out))], None
    if name == 'log':
        since = a.get('since')
        body, cursor = game.tail(int(a.get('lines', 60)), int(since) if since is not None else None)
        return [text('%s\n(cursor %d)' % (body, cursor))], None
    raise KeyError(name)


# ---- the protocol

def main():
    cfg = load_config()
    game = Game(cfg)
    out = sys.stdout

    def reply(msg_id, result=None, error=None):
        msg = {'jsonrpc': '2.0', 'id': msg_id}
        if error is not None:
            msg['error'] = error
        else:
            msg['result'] = result
        out.write(json.dumps(msg) + '\n')
        out.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        method, msg_id, params = msg.get('method'), msg.get('id'), msg.get('params') or {}
        if msg_id is None:
            continue            # a notification (initialized, cancelled): nothing to answer
        if method == 'initialize':
            asked = params.get('protocolVersion')
            reply(msg_id, {'protocolVersion': asked if asked in PROTOCOLS else PROTOCOLS[0], 'capabilities': {'tools': {}},
                           'serverInfo': {'name': 'jka-mcp', 'title': 'Jedi Academy', 'version': VERSION},
                           'instructions': INSTRUCTIONS})
        elif method == 'ping':
            reply(msg_id, {})
        elif method == 'tools/list':
            reply(msg_id, {'tools': TOOLS})
        elif method == 'tools/call':
            name = params.get('name')
            if not any(t['name'] == name for t in TOOLS):
                reply(msg_id, error={'code': -32602, 'message': 'unknown tool %s' % name})
                continue
            try:
                content, structured = call(game, cfg, name, params.get('arguments') or {})
                result = {'content': content, 'isError': False}
                if structured is not None:
                    result['structuredContent'] = structured
                reply(msg_id, result)
            except Exception as e:
                reply(msg_id, {'content': [text('%s: %s' % (type(e).__name__, e))], 'isError': True})
        else:
            reply(msg_id, error={'code': -32601, 'message': 'unknown method %s' % method})
    game.stop()


if __name__ == '__main__':
    main()
