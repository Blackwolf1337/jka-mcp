#!/usr/bin/env python3
"""jka-mcp: an MCP server that runs Jedi Knight: Jedi Academy (OpenJK based clients such as VisionJK) without a
window and lets an AI assistant test it: menus (a dump of their items, mouse, keys), gameplay (held buttons timed
by the game clock, the view, a dump of the level, world points projected to the screen), screenshots (cropped,
zoomed, in sequences, side by side), sound (recorded and measured), several clients and a local dedicated server,
real mouse and key input (XTEST) for windows the ui module does not own, and rebuilding menus, modules, the renderer
or the engine into the running game.

Clients run in gamescope's headless backend (no window). The server speaks the Model Context Protocol over stdio
(JSON-RPC 2.0, one message a line) and needs only the Python standard library; Pillow makes screenshots smaller and
does crops, sequences and comparisons, numpy does the analysis of sound.

Configuration: a JSON file named by JKA_MCP_CONFIG (default: jka-mcp.json next to this file), keys see CONFIG below.
The tool configure changes it for this server process only (every MCP client session runs its own), never the file.
"""
import base64
import collections
import ctypes
import ctypes.util
import errno
import glob
import io
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time

VERSION = '0.3.0'
PROTOCOLS = ('2025-11-25', '2025-06-18', '2025-03-26', '2024-11-05')

INSTRUCTIONS = (
    'Drives Jedi Academy (VisionJK) clients without a window, and a local dedicated server. launch starts a client '
    '(name it with "client" to run several; the default is "main"), quit ends it. Coordinates are pixels of the game '
    'window. Menus: menu_dump, click (by item name), key, mouse. Windows of the cgame (e.g. VisionJK\'s drawing board) '
    'get real input with input (pointer, buttons, wheel, keys with modifiers). configure points this server alone at '
    'another build (e.g. a worktree) without touching the shared configuration file. Gameplay: launch with map= (devmap, cheats on) or '
    'connect=, then wait kind=ingame, hold for buttons timed by the game clock, view to place the player, state for '
    'the player and the entities, project for world points on the screen. screenshot can crop, zoom or frame a world '
    'point; sequence takes several at game times; compare puts two side by side. Pass screenshot=true to an action to '
    'see the result in the same call. reload rebuilds menus, modules, the renderer or the engine.')

# what the configuration file may say, and what it is without it
CONFIG = {
    'game_dir': '',                 # folder with the executable of the game (and base/ with the assets)
    'binary': './visionjk.x86_64',  # the executable, relative to game_dir
    'server_binary': './visionjkded.x86_64',
    'home': '',                     # fs_homepath of the client "main" (configs, screenshots); others get <home>-<name>;
                                    #   empty: <game_dir>/../jka-mcp-home
    'fs_game': 'japlus',            # the mod folder the client ends up in (where screenshots/ is)
    'launcher': 'gamescope',        # 'gamescope' (headless, no window) or 'direct' (a window on the desktop)
    'env': {},                      # more environment, e.g. {"VK_DRIVER_FILES": "/usr/share/vulkan/icd.d/radeon_icd.json"}
    # (com_bootlogo 0: with only +set words the engine plays the opening logos, which never end without a window)
    'args': ['+set', 'com_bootlogo', '0', '+set', 'v_updateCheck', '0', '+set', 'cl_motd', '0', '+set', 'com_skipIntro', '1'],
    'quiet': {'s_volume': '0', 's_musicvolume': '0'},  # cvars of a launch without audio=true
    # cvars of every client launch: no console lines over the view (the dumps of the tools print there)
    'cvars': {'con_notifytime': '0'},
    'server_args': ['+set', 'sv_pure', '0', '+set', 'g_gametype', '0'],
    'server_port': 29070,           # where server_launch starts looking for a free UDP port
    'renderer': 'rd-vulkan-vision', # cl_renderer, empty: the game's own choice
    'presets': {                    # named sets of cvars a launch can ask for (preset=[...])
        'msaa8': {'r_ext_multisample': '8'},
        'no_spawn_protection': {'g_spawnInvulnerability': '0'},
    },
    'prepare': {                    # what prepare builds: a game folder of its own (never the folder someone plays from)
        'link': [],                 # [[source dir, destination dir under game_dir, glob], ...]: symlinks, e.g. the base pk3s
        'copy': [],                 # [[file, destination relative to game_dir], ...]: real copies (binary, renderers, modules)
    },
    'reload': {                     # what reload runs: for each kind a list of commands (argv lists), run in 'cwd'
        'cwd': '',
        'menus': [],                # e.g. [["python3", "tools/menus/build.py"]]
        'menus_copy': [],           # [[source dir, destination under home/<fs_game>], ...]: loose files the game reads first
        'menus_put': [],            # [[built file, destination relative to game_dir], ...], e.g. the asset pk3
        'modules': [],              # e.g. [["ninja", "-C", "build64", "uix86_64.so", "cgamex86_64.so"]]
        'modules_copy': [],         # [[built file, destination relative to game_dir], ...]
        'renderer': [],             # the renderer libraries: vid_restart after them
        'renderer_copy': [],
        'engine': [],               # the executable: the clients are relaunched (relaunch keeps map, place and view)
        'engine_copy': [],
    },
    'log_file': '',                 # where the output of the game goes as well; empty: <home>/jka-mcp.log
}

# key numbers of the game (codemp/ui/keycodes.h) for the names the tool 'key' takes
KEYS = {'shift': 1, 'ctrl': 2, 'alt': 3, 'backspace': 8, 'tab': 9, 'enter': 10, 'escape': 27, 'esc': 27, 'f1': 28,
        'space': 32, 'delete': 127, 'mwheelup': 137, 'mwheeldown': 139, 'mouse1': 141, 'mouse2': 142, 'home': 144,
        'pageup': 145, 'end': 157, 'pagedown': 158, 'up': 170, 'down': 171, 'left': 172, 'right': 173}


# what the tool configure changed for this process: another file, path prefixes replaced, keys set on top
OVERRIDES = {'file': None, 'replace': {}, 'set': {}}


def config_path(own=True):
    """The configuration file: the one configure chose (own), else JKA_MCP_CONFIG, else jka-mcp.json next to this file."""
    return ((own and OVERRIDES['file']) or os.environ.get('JKA_MCP_CONFIG')
            or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'jka-mcp.json'))


def merge(cfg, user):
    for k, v in user.items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k].update(v)
        else:
            cfg[k] = v


def replaced(v, pairs):
    """Every string of v that is one of the paths of pairs, or below one, with that part replaced (longest first)."""
    if isinstance(v, str):
        for old in sorted(pairs, key=len, reverse=True):
            o = old.rstrip('/')
            if v == o or v.startswith(o + '/'):
                return pairs[old].rstrip('/') + v[len(o):]
        return v
    if isinstance(v, list):
        return [replaced(x, pairs) for x in v]
    if isinstance(v, dict):
        return {k: replaced(x, pairs) for k, x in v.items()}
    return v


def load_config(own=True):
    """The configuration as this process uses it; own=False: the file alone, as other sessions see it."""
    path = config_path(own)
    cfg = json.loads(json.dumps(CONFIG))
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            merge(cfg, json.load(f))
    if own and OVERRIDES['replace']:
        cfg = replaced(cfg, OVERRIDES['replace'])
    if own and OVERRIDES['set']:
        merge(cfg, json.loads(json.dumps(OVERRIDES['set'])))
    if not cfg['home'] and cfg['game_dir']:
        cfg['home'] = os.path.join(os.path.dirname(os.path.abspath(cfg['game_dir'])), 'jka-mcp-home')
    if not cfg['log_file'] and cfg['home']:
        cfg['log_file'] = os.path.join(cfg['home'], 'jka-mcp.log')
    return cfg


def not_temporary(path, what):
    # (an OpenJK client refuses game code from a temporary folder: game and home must not be under /tmp)
    real = os.path.realpath(path)
    if real == '/tmp' or real.startswith('/tmp/') or real.startswith('/var/tmp/'):
        raise RuntimeError('%s must not be under /tmp (the client refuses game code from a temporary folder): %s' % (what, path))


def own_sessions():
    """The process sessions of the games of this server (each game runs in a session of its own)."""
    return {g.proc.pid for g in GAMES.values() if g.running()}


def foreign_processes(home=None, cwd=None):
    """Processes of other sessions (other MCP servers, a game started by hand) that use home (fs_homepath) or run in cwd:
    [(pid, command)]."""
    own = own_sessions()
    home = os.path.realpath(home) if home else None
    cwd = os.path.realpath(cwd) if cwd else None
    found = []
    for pid in os.listdir('/proc'):
        if not pid.isdigit() or int(pid) == os.getpid():
            continue
        try:
            with open('/proc/%s/stat' % pid) as f:
                # (after the command in parentheses: state, ppid, pgrp, session)
                sid = int(f.read().rsplit(')', 1)[1].split()[3])
            if sid in own:
                continue
            with open('/proc/%s/cmdline' % pid, 'rb') as f:
                argv = [x.decode('utf-8', 'replace') for x in f.read().split(b'\0')]
            hit = False
            if home:
                for i in range(len(argv) - 1):
                    if argv[i] == 'fs_homepath' and os.path.realpath(argv[i + 1]) == home:
                        hit = True
            if cwd and not hit and 'fs_homepath' in argv:
                hit = os.path.realpath('/proc/%s/cwd' % pid) == cwd
            if hit:
                found.append((int(pid), ' '.join(argv[:3])))
        except (OSError, IndexError, ValueError):
            continue
    return found


def port_free(port):
    """Nobody has the UDP port (IPv4, and IPv6 where there is any)."""
    for family, addr in ((socket.AF_INET, '0.0.0.0'), (socket.AF_INET6, '::')):
        try:
            s = socket.socket(family, socket.SOCK_DGRAM)
        except OSError:
            continue
        try:
            if family == socket.AF_INET6:
                s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            s.bind((addr, port))
        except OSError as e:
            if e.errno == errno.EADDRINUSE:
                return False
        finally:
            s.close()
    return True


def free_port(start):
    for port in range(int(start), int(start) + 200):
        if port_free(port):
            return port
    raise RuntimeError('no free UDP port from %s to %s' % (start, int(start) + 199))


class Game:
    """A running client or dedicated server: its process, its output (a queue of lines a reader thread fills) and its
    stdin, which the console of the engine reads line by line when it is not a terminal."""

    def __init__(self, cfg, name='main', server=False):
        self.cfg = cfg
        self.name = name
        self.server = server
        self.proc = None
        self.lines = collections.deque(maxlen=40000)
        self.count = 0              # lines seen so far, for 'since'
        self.cond = threading.Condition()
        self.shot = 0
        self.size = (0, 0)
        self.log = None
        self.opts = {}
        self.audio_path = None
        self.audio_pos = 0
        self.port = None            # the UDP port the server really has
        self.pointer = None         # input: where the pointer of the window is, in 640x480 units from the top left
        base = os.path.abspath(cfg['home'])
        self.home = base if name == 'main' and not server else '%s-%s' % (base, 'server' if server else name)

    # ---- the process

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def _launch_cfg(self, cvars):
        """The cvars of a launch go into a cfg that the command line execs: the engine takes at most 32 + commands from
        the command line and drops the rest without a word."""
        for k, v in cvars.items():
            if '"' in str(v) or '"' in k or ';' in k or not k:
                # (a cfg line has no way to put a double quote into a value)
                raise RuntimeError('cvar %s: a value may not contain a double quote: %r' % (k, v))
        text = ''.join('set %s "%s"\n' % (k, v) for k, v in cvars.items())
        for folder in ('base', self.cfg['fs_game']):
            os.makedirs(os.path.join(self.home, folder), exist_ok=True)
            with open(os.path.join(self.home, folder, 'jkamcp_launch.cfg'), 'w', encoding='utf-8') as f:
                f.write(text)
        return ['+exec', 'jkamcp_launch.cfg']

    def start(self, opts):
        if self.running():
            raise RuntimeError('%s is already running; quit it first' % self.label())
        cfg = self.cfg
        game_dir = os.path.abspath(cfg['game_dir'])
        if not os.path.isdir(game_dir):
            raise RuntimeError('game_dir is not set or not a folder: %r (prepare makes one)' % cfg['game_dir'])
        not_temporary(game_dir, 'game_dir')
        not_temporary(self.home, 'home')
        other = foreign_processes(home=self.home)
        if other:
            # (two games in one home overwrite each other's configuration and screenshots)
            raise RuntimeError('another game uses the home %s (pid %d: %s), e.g. one of another session: name this client '
                               'differently (client=) or give this server its own home (configure)' % ((self.home,) + other[0]))
        os.makedirs(os.path.join(self.home, cfg['fs_game']), exist_ok=True)
        if opts.get('map') and opts.get('connect'):
            raise RuntimeError('map= starts a level of its own, connect= joins a server: give one of them')
        self.opts = dict(opts)
        width, height = int(opts.get('width', 1920)), int(opts.get('height', 1080))
        cvars = {} if self.server else dict(cfg.get('cvars') or {})
        for p in opts.get('preset') or []:
            if p not in (cfg.get('presets') or {}):
                raise RuntimeError('no preset %r; there are: %s' % (p, ', '.join(sorted(cfg.get('presets') or {}))))
            cvars.update(cfg['presets'][p])
        env = dict(os.environ)
        env.update({k: str(v) for k, v in (cfg.get('env') or {}).items()})
        if self.server:
            # (the engine takes the next of 10 ports without a word when its own is taken: clients would join the
            # server of another session there)
            if opts.get('port'):
                self.port = int(opts['port'])
                if not port_free(self.port):
                    raise RuntimeError('the UDP port %d is taken (another server?); leave port out for a free one' % self.port)
            else:
                self.port = free_port(cfg.get('server_port') or 29070)
            args = [cfg['server_binary'], '+set', 'fs_homepath', self.home, '+set', 'dedicated', '1',
                    '+set', 'net_port', str(self.port)] + list(cfg.get('server_args') or [])
        else:
            args = [cfg['binary'], '+set', 'fs_homepath', self.home, '+set', 'r_mode', '-1', '+set', 'r_customwidth', str(width),
                    '+set', 'r_customheight', str(height), '+set', 'r_fullscreen', '0']
            if cfg.get('renderer'):
                args += ['+set', 'cl_renderer', opts.get('renderer') or cfg['renderer']]
            if opts.get('vk_debug'):
                # (the Vulkan validation layers, VisionJK: v_vkDebug; the messages are lines with VULKAN in the log)
                args += ['+set', 'v_vkDebug', '1']
            args += list(cfg.get('args') or [])
            if opts.get('audio'):
                # the sound into a file: SDL's disk driver writes what the game plays, S16LE stereo at the rate of the
                # device, in real time
                self.audio_path = os.path.join(self.home, 'jkamcp_audio.raw')
                if os.path.exists(self.audio_path):
                    os.remove(self.audio_path)
                self.audio_pos = 0
                env['SDL_AUDIODRIVER'] = 'disk'
                env['SDL_DISKAUDIOFILE'] = self.audio_path
                # (the volumes are archived: a quiet launch before leaves them at 0 in the config)
                cvars = dict({'s_volume': '1', 's_musicvolume': '0'}, **cvars)
            else:
                self.audio_path = None
                env['SDL_AUDIODRIVER'] = env.get('SDL_AUDIODRIVER', 'dummy')
                cvars = dict(cfg.get('quiet') or {}, **cvars)
        cvars.update({k: str(v) for k, v in (opts.get('cvars') or {}).items()})
        if cvars:
            args += self._launch_cfg(cvars)
        args += list(opts.get('extra') or [])
        if opts.get('map'):
            args += ['+devmap', opts['map']]
        elif opts.get('connect') and not self.server:
            # ("localhost" is the server inside the client's own process for the engine, not the one on the UDP port)
            where = opts['connect']
            if where.split(':')[0] in ('localhost', 'local'):
                # (the port of server_launch of this session)
                srv = GAMES.get('#server')
                port = srv.port if srv is not None and srv.running() and srv.port else cfg.get('server_port', 29070)
                where = '127.0.0.1:%s' % (where.split(':')[1] if ':' in where else port)
            args += ['+connect', where]
        if not self.server and cfg['launcher'] == 'gamescope':
            for k in ('DISPLAY', 'WAYLAND_DISPLAY'):
                env.pop(k, None)
            env['SDL_VIDEODRIVER'] = 'x11'
            cmd = ['gamescope', '--backend', 'headless', '-W', str(width), '-H', str(height), '-w', str(width),
                   '-h', str(height), '--'] + args
        else:
            cmd = args
        log_file = cfg.get('log_file')
        if log_file and (self.server or self.name != 'main'):
            log_file = os.path.join(self.home, 'jka-mcp.log')
        if log_file:
            os.makedirs(os.path.dirname(log_file), exist_ok=True)
            # (appended from launch to launch; past 20 MB it starts again, the old one kept as .old)
            if os.path.exists(log_file) and os.path.getsize(log_file) > LOG_MAX:
                os.replace(log_file, log_file + '.old')
        self.log = open(log_file, 'a', encoding='utf-8', errors='replace') if log_file else None
        with self.cond:
            self.lines.clear()
            self.count = 0
        self.proc = subprocess.Popen(cmd, cwd=game_dir, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, start_new_session=True)
        self.size = (width, height)
        threading.Thread(target=self._read, daemon=True).start()
        if not self.wait_for(r'Common Initialization Complete|execing visionjk\.cfg|execing autoexec|Hunk_Clear', 60, 0):
            out = self.tail(40)[0]
            self.stop()
            raise RuntimeError('%s did not start:\n%s' % (self.label(), out))
        if self.server:
            self.wait_for(r'Server: |------ Server Initialization|ClientBegin|Hitch warning', 30, 0)
            real = self.cvar('net_port')
            if real and real.isdigit():
                self.port = int(real)
            return '%s started (pid %d), port %d (connect=localhost joins it)' % (self.label(), self.proc.pid, self.port)
        if opts.get('map') or opts.get('connect'):
            return '%s started %dx%d (pid %d), loading %s; wait kind=ingame for the level' % (
                self.label(), width, height, self.proc.pid, opts.get('map') or opts.get('connect'))
        # the first menu (the logos are off, see 'args')
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
        return '%s started %dx%d (pid %d), open menus: %s' % (self.label(), width, height, self.proc.pid, ', '.join(menus) or 'none')

    def label(self):
        return 'the server' if self.server else 'client %s' % self.name

    def stop(self):
        if not self.proc:
            return 'not running'
        if self.proc.poll() is None:
            try:
                self.send('quit')
                self.proc.wait(timeout=8)
            except Exception:
                pass
        if self.proc.poll() is None or (self.cfg['launcher'] == 'gamescope' and not self.server):
            # gamescope may stay with its helpers (and hangs on exit with some drivers): end the whole session of the run
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
        self.pointer = None
        if self.log:
            self.log.close()
            self.log = None
        return '%s stopped (exit code %s)' % (self.label(), code)

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
            raise RuntimeError('%s is not running; use launch first' % self.label())
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

    def tail(self, n=50, since=None, pattern=None):
        with self.cond:
            lines = list(self.lines)
            if since is not None:
                lines = lines[-(self.count - since):] if self.count > since else []
            if pattern:
                rx = re.compile(pattern)
                lines = [line for line in lines if rx.search(line)]
            return '\n'.join(lines[-n:]), self.count

    def cvar(self, name, timeout=3.0):
        lines = self.command(name, until=r'^Cvar %s = |"%s" is|^%s is' % ((re.escape(name),) * 3), timeout=timeout)
        for line in lines:
            m = re.match(r'Cvar \S+ = "(.*?)"', line) or re.match(r'"\S+" is:"(.*?)"', line)
            if m:
                return m.group(1)
        return None

    # ---- what the tools build on

    def screenshot(self, timeout=15.0):
        self.shot += 1
        name = 'jkamcp_%d_%s_%d' % (os.getpid(), self.name, self.shot)
        m = self.mark()
        self.send('screenshot_png %s' % name)
        line = self.wait_for(r'Wrote screenshots/%s\.png' % name, timeout, m)
        if not line:
            raise RuntimeError('no screenshot was written:\n' + '\n'.join(self.since(m)[-15:]))
        found = glob.glob(os.path.join(self.home, '*', 'screenshots', name + '.png'))
        if not found:
            found = glob.glob(os.path.join(os.path.abspath(self.cfg['game_dir']), '*', 'screenshots', name + '.png'))
        if not found:
            raise RuntimeError('the game wrote %s.png, but it is not under %s' % (name, self.home))
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

    def level(self, radius=2048):
        """The level as the cgame sees it (VisionJK: v_levelDump): time, player, view, entities near the view."""
        lines = self.command('v_levelDump %s' % radius, until=r'^leveldump end', timeout=5)
        out = {'entities': []}
        for line in lines:
            if not line.startswith('leveldump '):
                continue
            parts = line.split(' ')
            if parts[1] == 'end':
                break
            rec = {}
            for kv in parts[2:]:
                k, _, val = kv.partition('=')
                if ',' in val:
                    try:
                        rec[k] = [float(a) for a in val.split(',')]
                        continue
                    except ValueError:
                        pass
                try:
                    rec[k] = int(val)
                except ValueError:
                    try:
                        rec[k] = float(val)
                    except ValueError:
                        rec[k] = val
            if parts[1] == 'entity':
                out['entities'].append(rec)
            else:
                out[parts[1]] = rec
        if 'time' not in out:
            raise RuntimeError('no answer to v_levelDump (it needs a cgame of VisionJK and a level): ' + ' | '.join(lines[-5:]))
        return out

    def game_time(self):
        return self.level(0)['time']['cg']

    def wait_game_ms(self, ms, timeout=None):
        """Waits until the clock of the cgame went on by ms (not frames: the frame rate changes)."""
        start = self.game_time()
        end = time.time() + (timeout if timeout is not None else max(5.0, ms / 1000.0 * 4 + 5))
        while time.time() < end:
            now = self.game_time()
            if now - start >= ms:
                return now - start
            time.sleep(min(0.05, max(0.005, (ms - (now - start)) / 2000.0)))
        raise RuntimeError('the game clock did not go on by %d ms (paused, or no level?)' % ms)

    def project(self, point):
        lines = self.command('v_project %g %g %g' % tuple(point), until=r'^project ', timeout=3)
        for line in lines:
            if line.startswith('project behind'):
                return None
            m = re.match(r'project (\S+) (\S+)', line)
            if m:
                return float(m.group(1)), float(m.group(2))
        raise RuntimeError('no answer to v_project (it needs a cgame of VisionJK and a level)')


GAMES = {}          # the clients by name, and the server under '#server'
LOG_MAX = 20 * 1024 * 1024


def game_of(cfg, a, create=False, server=False):
    key = '#server' if server else (a.get('client') or 'main')
    g = GAMES.get(key)
    if g is None:
        if not create:
            raise RuntimeError('no %s; use %s first' % ('server' if server else 'client %r' % key, 'server_launch' if server else 'launch'))
        g = GAMES[key] = Game(cfg, key, server)
    return g


# ---- pictures

def _pil():
    try:
        from PIL import Image, ImageDraw
        return Image, ImageDraw
    except ImportError:
        return None, None


def picture(data, crop=None, zoom=1.0):
    Image, _ = _pil()
    if Image is None:
        if crop or zoom != 1.0:
            raise RuntimeError('crop and zoom need Pillow (pip install pillow)')
        return None
    im = Image.open(io.BytesIO(data)).convert('RGB')
    if crop:
        x, y, w, h = [int(round(v)) for v in crop]
        x, y = max(0, x), max(0, y)
        im = im.crop((x, y, min(im.width, x + max(1, w)), min(im.height, y + max(1, h))))
    if zoom and zoom != 1.0:
        im = im.resize((max(1, round(im.width * zoom)), max(1, round(im.height * zoom))), Image.LANCZOS)
    return im


def image_content(data, max_width=1280, quality=85, crop=None, zoom=1.0, im=None, fit=False):
    """A screenshot as MCP image content: cropped, zoomed, smaller and as JPEG where Pillow is there, else the PNG.
    fit: a smaller picture is made as wide as max_width (as high, if it is higher than wide)."""
    if im is None:
        im = picture(data, crop, zoom)
    if im is None:
        return {'type': 'image', 'data': base64.b64encode(data).decode(), 'mimeType': 'image/png'}
    Image, _ = _pil()
    if fit and max_width and max(im.width, im.height) < max_width:
        f = max_width / float(max(im.width, im.height))
        im = im.resize((round(im.width * f), round(im.height * f)), Image.LANCZOS)
    elif max_width and im.width > max_width:
        im = im.resize((max_width, round(im.height * max_width / im.width)), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, 'JPEG', quality=quality)
    return {'type': 'image', 'data': base64.b64encode(buf.getvalue()).decode(), 'mimeType': 'image/jpeg'}


def strip(images, labels):
    """Pictures side by side with a label over each."""
    Image, ImageDraw = _pil()
    if Image is None:
        raise RuntimeError('sequences and comparisons need Pillow (pip install pillow)')
    h = max(i.height for i in images)
    images = [i if i.height == h else i.resize((round(i.width * h / i.height), h), Image.LANCZOS) for i in images]
    out = Image.new('RGB', (sum(i.width for i in images) + 8 * (len(images) - 1), h + 22), (255, 0, 255))
    draw = ImageDraw.Draw(out)
    x = 0
    for im, label in zip(images, labels):
        out.paste(im, (x, 22))
        draw.rectangle((x, 0, x + im.width, 21), fill=(0, 0, 0))
        draw.text((x + 4, 4), label, fill=(255, 255, 255))
        x += im.width + 8
    return out


SHOTS = collections.OrderedDict()   # the last screenshots by id, for compare
SHOT_COUNT = 0


def keep_shot(data, label):
    global SHOT_COUNT
    SHOT_COUNT += 1
    sid = 's%d' % SHOT_COUNT
    SHOTS[sid] = (data, label)
    while len(SHOTS) > 30:
        SHOTS.popitem(last=False)
    return sid


def framing(game, a):
    """The rectangle a screenshot is cut to: crop=[x,y,w,h], or around=[x,y,z] (a point of the level) with size=[w,h]
    in pixels, or with extent (world units): the box of that half size around the point, as large as it is on the
    window."""
    if a.get('around'):
        p = game.project(a['around'])
        if p is None:
            raise RuntimeError('the point %s is behind the view' % (a['around'],))
        if a.get('extent'):
            e = float(a['extent'])
            x, y, z = a['around']
            corners = [game.project([x + dx, y + dy, z + dz]) for dx in (-e, e) for dy in (-e, e) for dz in (-e, e)]
            corners = [c for c in corners if c is not None]
            if not corners:
                raise RuntimeError('the box around %s is behind the view' % (a['around'],))
            x0, x1 = min(c[0] for c in corners), max(c[0] for c in corners)
            y0, y1 = min(c[1] for c in corners), max(c[1] for c in corners)
            # (a corner behind the view is left out: at least the point itself with a margin)
            w, h = max(x1 - x0, 32.0), max(y1 - y0, 32.0)
            x0, y0 = min(x0, p[0] - w / 2.0), min(y0, p[1] - h / 2.0)
            return [x0, y0, w, h], p
        w, h = a.get('size') or [400, 300]
        return [p[0] - w / 2.0, p[1] - h / 2.0, w, h], p
    return a.get('crop'), None


# ---- sound

def analyse_audio(raw, rate, window_ms=100, peaks=3):
    """Loudness (dBFS) and the strongest frequencies of S16LE stereo sound, over windows of window_ms."""
    try:
        import numpy as np
    except ImportError:
        raise RuntimeError('the analysis of sound needs numpy (pip install numpy)')
    a = np.frombuffer(raw[:len(raw) // 4 * 4], dtype='<i2').reshape(-1, 2).astype(np.float64) / 32768.0
    mono = a.mean(axis=1)
    n = max(1, int(rate * window_ms / 1000))
    rows = []
    for i in range(0, len(mono) - n + 1, n):
        w = mono[i:i + n]
        rms = float(np.sqrt(np.mean(w * w)))
        db = 20 * np.log10(rms) if rms > 1e-9 else -120.0
        spec = np.abs(np.fft.rfft(w * np.hanning(n)))
        freqs = np.fft.rfftfreq(n, 1.0 / rate)
        top = []
        if rms > 1e-6:
            order = np.argsort(spec[1:])[::-1] + 1
            for k in order:
                f = float(freqs[k])
                if all(abs(f - t) > rate / n * 3 for t in top):
                    top.append(f)
                if len(top) >= peaks:
                    break
        rows.append((i / rate, db, top))
    whole = float(np.sqrt(np.mean(mono * mono))) if len(mono) else 0.0
    peak = float(np.max(np.abs(a))) if len(a) else 0.0
    return rows, (20 * np.log10(whole) if whole > 1e-9 else -120.0), (20 * np.log10(peak) if peak > 1e-9 else -120.0)


# ---- real input (XTEST) into the X display of a client's gamescope

# the names the tool input takes for keys, as X keysyms (f1 to f12 and single characters by rule)
XKEYS = {'enter': 'Return', 'return': 'Return', 'esc': 'Escape', 'escape': 'Escape', 'tab': 'Tab',
         'backspace': 'BackSpace', 'delete': 'Delete', 'insert': 'Insert', 'space': 'space', 'up': 'Up', 'down': 'Down',
         'left': 'Left', 'right': 'Right', 'home': 'Home', 'end': 'End', 'pageup': 'Prior', 'pagedown': 'Next',
         'ctrl': 'Control_L', 'control': 'Control_L', 'shift': 'Shift_L', 'alt': 'Alt_L', 'console': 'grave'}
XLIB = None


def xlib():
    """libX11 and libXtst through ctypes (gamescope needs both, so they are there where it is)."""
    global XLIB
    if XLIB is None:
        try:
            x11 = ctypes.CDLL(ctypes.util.find_library('X11') or 'libX11.so.6')
            xtst = ctypes.CDLL(ctypes.util.find_library('Xtst') or 'libXtst.so.6')
        except OSError as e:
            raise RuntimeError('input needs libX11 and libXtst: %s' % e)
        vp, ul, ci, cu = ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_uint
        for name, res, args in (('XOpenDisplay', vp, [ctypes.c_char_p]), ('XCloseDisplay', ci, [vp]),
                                ('XSync', ci, [vp, ci]), ('XStringToKeysym', ul, [ctypes.c_char_p]),
                                ('XKeysymToKeycode', ctypes.c_ubyte, [vp, ul]),
                                ('XkbKeycodeToKeysym', ul, [vp, ctypes.c_ubyte, ci, ci])):
            getattr(x11, name).restype, getattr(x11, name).argtypes = res, args
        for name, args in (('XTestFakeRelativeMotionEvent', [vp, ci, ci, ul]), ('XTestFakeButtonEvent', [vp, cu, ci, ul]),
                           ('XTestFakeKeyEvent', [vp, cu, ci, ul])):
            getattr(xtst, name).restype, getattr(xtst, name).argtypes = ci, args
        # (Xlib's own handler of an error ends the whole process: this one only notes it)
        handler = ctypes.CFUNCTYPE(ci, vp, vp)(lambda d, e: 0)
        x11.XSetErrorHandler.restype, x11.XSetErrorHandler.argtypes = vp, [vp]
        x11.XSetErrorHandler(ctypes.cast(handler, vp))
        XLIB = (x11, xtst, handler)
    return XLIB


def game_environ(game):
    """The environment of the game process itself (not of gamescope): its DISPLAY is gamescope's own X server."""
    exe = os.path.basename(game.cfg['binary'])
    for pid in os.listdir('/proc'):
        if not pid.isdigit():
            continue
        try:
            with open('/proc/%s/stat' % pid) as f:
                if int(f.read().rsplit(')', 1)[1].split()[3]) != game.proc.pid:
                    continue
            with open('/proc/%s/cmdline' % pid, 'rb') as f:
                if os.path.basename(f.read().split(b'\0')[0].decode('utf-8', 'replace')) != exe:
                    continue
            with open('/proc/%s/environ' % pid, 'rb') as f:
                env = dict(x.decode('utf-8', 'replace').partition('=')[::2] for x in f.read().split(b'\0') if b'=' in x)
            if env.get('DISPLAY'):
                return env
        except (OSError, IndexError, ValueError):
            continue
    raise RuntimeError('found no %s with a DISPLAY under gamescope (pid %d)' % (exe, game.proc.pid))


class XInput:
    """One connection to the X display of a client, for the length of one call (it ends with the game)."""

    def __init__(self, game):
        if game.cfg['launcher'] != 'gamescope' or game.server:
            # (with launcher direct the display is the desktop: the pointer of the person at it would move)
            raise RuntimeError('input needs a client launched with launcher gamescope')
        if not game.running():
            raise RuntimeError('%s is not running; use launch first' % game.label())
        self.x11, self.xtst, _ = xlib()
        env = game_environ(game)
        if env.get('XAUTHORITY'):
            os.environ['XAUTHORITY'] = env['XAUTHORITY']
        self.display = env['DISPLAY']
        self.dpy = self.x11.XOpenDisplay(self.display.encode())
        if not self.dpy:
            raise RuntimeError('could not open the X display %s of the game' % self.display)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.x11.XCloseDisplay(self.dpy)

    def sync(self, pause=0.0):
        self.x11.XSync(self.dpy, 0)
        if pause:
            time.sleep(pause)

    def move(self, dx, dy, step=40):
        """Relative motion in steps: SDL's relative mode puts the pointer back in the middle after every event; one
        big jump would stop at the edge of the screen."""
        while dx or dy:
            sx, sy = max(-step, min(step, dx)), max(-step, min(step, dy))
            self.xtst.XTestFakeRelativeMotionEvent(self.dpy, sx, sy, 0)
            self.sync(0.012)
            dx, dy = dx - sx, dy - sy

    def button(self, number, down):
        self.xtst.XTestFakeButtonEvent(self.dpy, number, 1 if down else 0, 0)
        self.sync(0.03)

    def keycode(self, keysym):
        code = self.x11.XKeysymToKeycode(self.dpy, keysym)
        if not code:
            raise RuntimeError('the keyboard map of %s has no key for keysym 0x%x' % (self.display, keysym))
        # (a character on the second level of its key needs shift, e.g. upper case or "!")
        return code, self.x11.XkbKeycodeToKeysym(self.dpy, code, 0, 0) != keysym

    def keysym(self, name):
        if len(name) == 1:
            # (Latin-1 characters are their own keysyms; the names of the others, e.g. "a", "1", work too)
            return ord(name) if ord(name) < 256 else 0x01000000 + ord(name)
        n = XKEYS.get(name.lower()) or (name.upper() if re.match(r'(?i)f\d{1,2}$', name) else name)
        sym = self.x11.XStringToKeysym(n.encode())
        if not sym:
            raise RuntimeError('unknown key %r; use one character, f1-f12, an X keysym name or one of %s'
                               % (name, ', '.join(sorted(XKEYS))))
        return sym

    def key(self, combo):
        """A key with modifiers held around it, e.g. "ctrl+v", "shift+tab", "enter", "A"."""
        parts = combo.split('+') if combo != '+' else ['+']
        if parts[-1] == '' and len(parts) > 1:
            parts = parts[:-2] + ['+']      # ("ctrl++")
        mods = [self.keycode(self.keysym(m))[0] for m in parts[:-1]]
        code, shift = self.keycode(self.keysym(parts[-1]))
        if shift:
            mods.append(self.keycode(self.keysym('shift'))[0])
        for m in mods:
            self.xtst.XTestFakeKeyEvent(self.dpy, m, 1, 0)
        self.sync(0.02)
        self.xtst.XTestFakeKeyEvent(self.dpy, code, 1, 0)
        self.sync(0.03)
        self.xtst.XTestFakeKeyEvent(self.dpy, code, 0, 0)
        self.sync(0.02)
        for m in reversed(mods):
            self.xtst.XTestFakeKeyEvent(self.dpy, m, 0, 0)
        self.sync(0.02)


def pointer_to(game, xi, x, y, home, step_px):
    """The pointer of the window to window pixel x, y. The cgame and the ui keep it in 640x480 units (wider windows
    add a margin left and right) and take the raw motion of the mouse: one X pixel is one unit, so a unit is
    min(width/640, height/480) window pixels. home first runs it into the top left corner, where it stops."""
    w, h = game.size
    scale = float(step_px) if step_px else min(w / 640.0, h / 480.0)
    if home or game.pointer is None:
        xi.move(-int(w / scale) - 20, -int(h / scale) - 20)
        game.pointer = (0, 0)
    tx, ty = int(round(x / scale)), int(round(y / scale))
    xi.move(tx - game.pointer[0], ty - game.pointer[1])
    game.pointer = (tx, ty)
    return tx * scale, ty * scale


# ---- the tools

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


CLIENT = {'client': {'type': 'string', 'default': 'main', 'description': 'which client (launch names them)'}}
SHOT = {'screenshot': {'type': 'boolean', 'default': False, 'description': 'also return a screenshot taken after the action'}}
FRAME = {'crop': {'type': 'array', 'items': {'type': 'number'}, 'description': '[x, y, w, h] in window pixels'},
         'around': {'type': 'array', 'items': {'type': 'number'}, 'description': '[x, y, z]: a point of the level to frame'},
         'size': {'type': 'array', 'items': {'type': 'number'}, 'description': '[w, h] of the frame around the point', 'default': [400, 300]},
         'extent': {'type': 'number', 'description': 'instead of size: half the size in world units of a box around the point; '
                                                     'the frame is that box as large as it is on the window (full resolution)'},
         'zoom': {'type': 'number', 'default': 1}}
RO = {'readOnlyHint': True, 'openWorldHint': False}
ACT = {'readOnlyHint': False, 'destructiveHint': False, 'openWorldHint': False}
P = dict  # (short for the property dicts below)

TOOLS = [
    {'name': 'launch', 'title': 'Start a client',
     'description': 'Start a client without a window (gamescope headless) and wait for its first menu, or with map= (devmap, '
                    'cheats on) or connect= (an address, e.g. "localhost" for server_launch) for the level: then use wait '
                    'kind=ingame. client: a name to run several. preset: named cvar sets of the configuration; cvars: more '
                    '(written to a cfg, the engine drops command line words past 32). audio: record the sound (tool audio). '
                    'vk_debug: Vulkan validation (log pattern=VULKAN).',
     'inputSchema': schema(dict(CLIENT, width=P(type='integer', default=1920), height=P(type='integer', default=1080),
                                map=P(type='string'), connect=P(type='string'), renderer=P(type='string'),
                                preset=P(type='array', items=P(type='string')), cvars=P(type='object'),
                                audio=P(type='boolean', default=False), vk_debug=P(type='boolean', default=False),
                                extra=P(type='array', items=P(type='string')))),
     'annotations': ACT},
    {'name': 'quit', 'title': 'End a client', 'description': 'End a client (all: every client and the server).',
     'inputSchema': schema(dict(CLIENT, all=P(type='boolean', default=False))),
     'annotations': {'readOnlyHint': False, 'destructiveHint': True, 'idempotentHint': True, 'openWorldHint': False}},
    {'name': 'server_launch', 'title': 'Start a local server',
     'description': 'Start the dedicated server (no window) on map, for clients to connect=localhost. gametype: g_gametype '
                    '(0 FFA, 3 duel, 6 team FFA, 7 siege, 8 CTF). Its console: console with server=true.',
     'inputSchema': schema(P(map=P(type='string'), gametype=P(type='integer', default=0), port=P(type='integer'),
                             cvars=P(type='object'), extra=P(type='array', items=P(type='string'))), ['map']),
     'annotations': ACT},
    {'name': 'server_quit', 'title': 'End the server', 'description': 'End the dedicated server.', 'inputSchema': schema(),
     'annotations': {'readOnlyHint': False, 'destructiveHint': True, 'idempotentHint': True, 'openWorldHint': False}},
    {'name': 'console', 'title': 'Console command',
     'description': 'Run console commands (one a line) and return what was printed after them. until: a regular expression; '
                    'returns as soon as a printed line matches it (or after timeout seconds). Without until it waits wait '
                    'seconds. server=true: the console of the dedicated server.',
     'inputSchema': schema(dict(CLIENT, command=P(type='string'), wait=P(type='number', default=0.5), until=P(type='string'),
                                timeout=P(type='number', default=10), server=P(type='boolean', default=False)), ['command']),
     'annotations': ACT},
    {'name': 'wait', 'title': 'Wait for a state',
     'description': 'Wait until: kind=ingame (the level is loaded and the player plays; join=true picks team free and closes '
                    'the menus), kind=log (a printed line matches pattern), kind=cvar (name has value), kind=time (the game '
                    'clock went on by ms; not frames, the frame rate changes).',
     'inputSchema': schema(dict(CLIENT, kind=P(type='string', enum=['ingame', 'log', 'cvar', 'time']), pattern=P(type='string'),
                                name=P(type='string'), value=P(type='string'), ms=P(type='integer'), join=P(type='boolean', default=True),
                                timeout=P(type='number', default=60), server=P(type='boolean', default=False)), ['kind']),
     'annotations': ACT},
    {'name': 'screenshot', 'title': 'Screenshot',
     'description': 'Picture of the window now, as JPEG. crop [x,y,w,h] or around a point of the level [x,y,z] (size [w,h] '
                    'in pixels, or extent in world units for a box around it), zoom, max_width (0: full size), fit (a small '
                    'frame scaled up to max_width). A frame is cut at full resolution. Returns an id for compare and the '
                    'frame. Pixel coordinates always refer to the full window.',
     'inputSchema': schema(dict(CLIENT, max_width=P(type='integer', default=1280), quality=P(type='integer', default=85),
                                fit=P(type='boolean', default=False), **FRAME)),
     'annotations': RO},
    {'name': 'sequence', 'title': 'Screenshots at game times',
     'description': 'Several screenshots at game times after now (at_ms, also called times, e.g. [0, 100, 250, 500]), side by '
                    'side in one picture; while they are taken the buttons of hold run if given (e.g. ["+attack"]). '
                    'crop/around/extent/zoom as for screenshot (around is projected once, at the start).',
     'inputSchema': schema(dict(CLIENT, at_ms=P(type='array', items=P(type='integer'), description='game times in ms after now'),
                                times=P(type='array', items=P(type='integer'), description='the same as at_ms'),
                                hold=P(type='array', items=P(type='string')),
                                max_width=P(type='integer', default=1600), **FRAME)),
     'annotations': ACT},
    {'name': 'compare', 'title': 'Two screenshots side by side',
     'description': 'Two screenshots of this session (ids from screenshot or sequence) side by side, cropped the same if crop is given.',
     'inputSchema': schema(P(a=P(type='string'), b=P(type='string'), crop=FRAME['crop'], zoom=FRAME['zoom'],
                             max_width=P(type='integer', default=1600)), ['a', 'b']),
     'annotations': RO},
    {'name': 'state', 'title': 'Player, view and entities',
     'description': 'The level as the cgame sees it (VisionJK v_levelDump): game time, own player (origin, velocity, angles, '
                    'health, weapon, third person), the view (origin, angles, fov, rect) and the entities within radius of the '
                    'view (type, origin, distance, model). format concise or json.',
     'inputSchema': schema(dict(CLIENT, radius=P(type='number', default=1024), format=P(type='string', enum=['concise', 'json'], default='concise'))),
     'annotations': RO},
    {'name': 'project', 'title': 'World points to window pixels',
     'description': 'Where points of the level ([[x,y,z], ...]) are on the window, in pixels (null: behind the view).',
     'inputSchema': schema(dict(CLIENT, points=P(type='array', items=P(type='array', items=P(type='number'))))),
     'annotations': RO},
    {'name': 'hold', 'title': 'Hold buttons',
     'description': 'Press buttons (e.g. ["+forward", "+attack", "+v_water"], "+" commands) for ms of game time (the clock '
                    'of the cgame, not frames), then let them go.',
     'inputSchema': schema(dict(CLIENT, buttons=P(type='array', items=P(type='string')), ms=P(type='integer', default=500), **SHOT), ['buttons']),
     'annotations': ACT},
    {'name': 'view', 'title': 'Place the player',
     'description': 'Put the player at origin [x,y,z] looking at yaw and pitch (what is not given stays), without the push of '
                    'a teleport unless still=false (setviewpos; needs cheats, e.g. launch map=). third: third person on/off, '
                    'range/angle for its camera.',
     'inputSchema': schema(dict(CLIENT, origin=P(type='array', items=P(type='number')), yaw=P(type='number'),
                                pitch=P(type='number'), still=P(type='boolean', default=True), third=P(type='boolean'), range=P(type='number'),
                                angle=P(type='number'), **SHOT)),
     'annotations': ACT},
    {'name': 'audio', 'title': 'Record the sound',
     'description': 'Record seconds of the sound of a client launched with audio=true and return its loudness (dBFS) and the '
                    'strongest frequencies per window. While it records, the buttons of hold run if given.',
     'inputSchema': schema(dict(CLIENT, seconds=P(type='number', default=2), window_ms=P(type='integer', default=100),
                                hold=P(type='array', items=P(type='string')))),
     'annotations': ACT},
    {'name': 'menu_dump', 'title': 'List menu items',
     'description': 'The open menus and their visible items: name, type, rect x,y,w,h in window pixels, flags (F focus, M mouse over, '
                    'D decoration, X disabled), cvar=value and text. filter: only items whose name, text or cvar contains it. '
                    'format: "concise" (one line per item, default) or "json" (also as structuredContent).',
     'inputSchema': schema(dict(CLIENT, filter=P(type='string'), format=P(type='string', enum=['concise', 'json'], default='concise'),
                                decorations=P(type='boolean', default=False, description='include items flagged D'))),
     'annotations': RO},
    {'name': 'mouse', 'title': 'Mouse',
     'description': 'Move the menu pointer to x, y (window pixels) and optionally click, hold, release, wheelup or wheeldown there.',
     'inputSchema': schema(dict(CLIENT, x=P(type='number'), y=P(type='number'),
                                action=P(type='string', enum=['move', 'click', 'hold', 'release', 'wheelup', 'wheeldown'], default='move'),
                                **SHOT), ['x', 'y']),
     'annotations': ACT},
    {'name': 'input', 'title': 'Real mouse and keys',
     'description': 'Real input through the X display of the client (XTEST), for what mouse and key do not reach: windows of the '
                    'cgame (VisionJK drawing board, boombox, model browser) and the game itself. x, y: move the pointer to that '
                    'window pixel (home=true first runs it into the top left corner, so the place is exact; false moves on from '
                    'the last place of this tool). action there: click, rightclick, hold, release (button 1, or button 1-3), '
                    'wheelup, wheeldown (count times). key: a key with modifiers, e.g. "enter", "esc", "ctrl+v", "shift+tab", '
                    '"f1", "A". text: typed character by character. Order: pointer, action, key, text. step_px: window pixels '
                    'per unit of the pointer if a window does not use 640x480 units (default min(width/640, height/480)).',
     'inputSchema': schema(dict(CLIENT, x=P(type='number'), y=P(type='number'), home=P(type='boolean', default=True),
                                action=P(type='string', enum=['move', 'click', 'rightclick', 'hold', 'release', 'wheelup', 'wheeldown'],
                                         default='move'),
                                button=P(type='integer', default=1), count=P(type='integer', default=1),
                                key=P(type='string'), text=P(type='string'), step_px=P(type='number'), **SHOT)),
     'annotations': ACT},
    {'name': 'click', 'title': 'Click item',
     'description': 'Click the middle of a menu item found by its exact name or text, else the first item whose text contains the words '
                    '(topmost menu first). menu: limit the search to one menu.',
     'inputSchema': schema(dict(CLIENT, item=P(type='string'), menu=P(type='string'), **SHOT), ['item']),
     'annotations': ACT},
    {'name': 'key', 'title': 'Key or text',
     'description': 'Press a key in the menus, key: one character or one of ' + ', '.join(sorted(KEYS)) + '. text: type a string '
                    'into the focused field. Both may be given (key first).',
     'inputSchema': schema(dict(CLIENT, key=P(type='string'), text=P(type='string'), **SHOT)),
     'annotations': ACT},
    {'name': 'reload', 'title': 'Rebuild and reload',
     'description': 'Rebuild from source and load into the running clients. menus: menu build, loose menu files, ui_load. modules: '
                    'ui/cgame/game libraries, vid_restart. renderer: renderer libraries, vid_restart (rebuild modules too when '
                    'shared structures changed). engine: the executable, the clients are relaunched keeping map, place and view. '
                    'Files are put in place by copy and rename.',
     'inputSchema': schema(P(what=P(type='string', enum=['menus', 'modules', 'renderer', 'engine']))),
     'annotations': {'readOnlyHint': False, 'destructiveHint': False, 'idempotentHint': True, 'openWorldHint': False}},
    {'name': 'relaunch', 'title': 'Restart a client in place',
     'description': 'Restart a client with the same launch options, back on its map at its place and view (needs a level loaded with '
                    'map=), keeping the cvars named in keep.',
     'inputSchema': schema(dict(CLIENT, keep=P(type='array', items=P(type='string')))),
     'annotations': ACT},
    {'name': 'configure', 'title': 'Configuration of this session',
     'description': 'Change the configuration for this MCP server only (every session runs its own; the shared file is not '
                    'touched, other sessions keep their build). file: another JSON file. replace: {"old path": "new path"} '
                    'for every path in it at or below the old one, e.g. the main clone by a worktree; game_dir and home need '
                    'their own replace (or set) so prepare does not fill the folder of another session. set: keys on top '
                    '(game_dir, home, server_port, prepare, reload, ...; null drops one). reset: back to the file. Without '
                    'arguments it shows the configuration. No client or server may run. reload and prepare keep it.',
     'inputSchema': schema(P(file=P(type='string'), replace=P(type='object'), set=P(type='object'),
                             reset=P(type='boolean', default=False))),
     'annotations': {'readOnlyHint': False, 'destructiveHint': False, 'idempotentHint': True, 'openWorldHint': False}},
    {'name': 'prepare', 'title': 'Make the test game folder',
     'description': 'Build the game folder of the configuration (game_dir) from prepare.link (symlinks, e.g. the base pk3s) and '
                    'prepare.copy (real copies of the executable, renderers, modules; the engine loads the renderer from next to '
                    'its real path). Never writes into the source folders. No client may run, also none of another session '
                    'from game_dir (configure gives this session a folder of its own).',
     'inputSchema': schema(),
     'annotations': {'readOnlyHint': False, 'destructiveHint': False, 'idempotentHint': True, 'openWorldHint': False}},
    {'name': 'log', 'title': 'Game output',
     'description': 'Lines the game printed: the last "lines", with since (the cursor a previous call returned) only newer ones, '
                    'with pattern only matching ones (e.g. "VULKAN" for the validation layers).',
     'inputSchema': schema(dict(CLIENT, lines=P(type='integer', default=60), since=P(type='integer'), pattern=P(type='string'),
                                server=P(type='boolean', default=False))),
     'annotations': RO},
]


def run_commands(cfg, commands):
    out = []
    cwd = cfg['reload'].get('cwd') or None
    for argv in commands:
        r = subprocess.run(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=1800)
        if r.returncode != 0:
            out.append('$ %s (exit %d)\n%s' % (' '.join(argv), r.returncode, r.stdout[-4000:]))
            raise RuntimeError('\n'.join(out))
        warnings = len(re.findall(r'warning:', r.stdout))
        out.append('$ %s (exit 0%s)' % (' '.join(argv), ', %d warnings' % warnings if warnings else ''))
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


def level_text(d):
    fmt = lambda v: ','.join('%g' % x for x in v) if isinstance(v, list) else str(v)  # noqa: E731
    out = []
    for k in ('time', 'player', 'view'):
        if k in d:
            out.append('%s %s' % (k, ' '.join('%s=%s' % (kk, fmt(vv)) for kk, vv in d[k].items())))
    for e in sorted(d['entities'], key=lambda e: e.get('dist', 0)):
        out.append('  #%s %s %s dist=%s weapon=%s %s' % (e.get('num'), e.get('type'), fmt(e.get('origin', [])), e.get('dist'),
                                                       e.get('weapon'), e.get('model')))
    return '\n'.join(out)


def after(game, a, msg):
    out = [text(msg)]
    if a.get('screenshot'):
        time.sleep(0.25)    # (a few frames for the game to answer)
        data = game.screenshot()
        sid = keep_shot(data, '%s %s' % (game.name, msg[:40]))
        out[0] = text('%s (screenshot %s)' % (msg, sid))
        out.append(image_content(data, 1280, 85))
    return out


def press(game, buttons, down):
    cmds = []
    for b in buttons:
        b = b.strip()
        if not b:
            continue
        name = b[1:] if b[0] in '+-' else b
        cmds.append(('+' if down else '-') + name)
    if cmds:
        game.send('\n'.join(cmds))


def wait_ingame(game, join, timeout):
    """Until the level is loaded and the player plays: team free and the menus closed when join."""
    end = time.time() + timeout
    joined = 0.0        # when team free went out last: again every 3 s (one sent while connecting gets lost)
    last = None
    while time.time() < end:
        if not game.running():
            raise RuntimeError('%s ended:\n%s' % (game.label(), game.tail(20)[0]))
        try:
            d = game.level(0)
        except RuntimeError:
            time.sleep(0.5)
            continue
        last = d
        if d['player'].get('spectating', 1) == 0 and d['time'].get('server', 0) > 0:
            if join:
                try:
                    if game.dump()['menus']:
                        game.send('v_uiKey 27')
                        time.sleep(0.3)
                        continue
                except RuntimeError:
                    pass
            return d
        if join and time.time() - joined > 3.0:
            game.send('team free')
            joined = time.time()
        elif join:
            try:
                if game.dump()['menus']:
                    game.send('v_uiKey 27')
            except RuntimeError:
                pass
        time.sleep(0.5)
    raise RuntimeError('not in the game after %ds%s' % (timeout, '' if last is None else ': ' + level_text(last).splitlines()[1]))


def do_reload(cfg, what):
    r = cfg['reload']
    out = []
    clients = [g for k, g in GAMES.items() if not g.server and g.running()]
    if what == 'menus':
        out.append(run_commands(cfg, r.get('menus') or []))
        pairs = r.get('menus_copy') or []
        if pairs and isinstance(pairs[0], str):
            pairs = [pairs]     # (one [source, destination] or a list of them)
        # (into the home of every client of this session, running or not, and of main: the server has no menus)
        homes = {g.home: g for g in GAMES.values() if not g.server}
        homes.setdefault(Game(cfg).home, None)
        for home in homes:
            for src, dst in pairs:
                dst = os.path.join(home, cfg['fs_game'], dst)
                n = 0
                for root, _, files in os.walk(src):
                    for f in files:
                        rel = os.path.relpath(os.path.join(root, f), src)
                        put_file(os.path.join(root, f), os.path.join(dst, rel))
                        n += 1
                out.append('copied %d files to %s' % (n, dst))
        for src, dst in r.get('menus_put') or []:
            put_file(src, os.path.join(os.path.abspath(cfg['game_dir']), dst))
            out.append('put %s (read at the next launch)' % dst)
        for g in clients:
            out.append('%s: %s' % (g.name, ui_load(g)))
        return '\n'.join(out)
    out.append(run_commands(cfg, r.get(what) or []))
    for src, dst in r.get(what + '_copy') or []:
        put_file(src, os.path.join(os.path.abspath(cfg['game_dir']), dst))
        out.append('put %s' % dst)
    if what in ('modules', 'renderer'):
        for g in clients:
            out.append('%s: %s' % (g.name, '\n'.join(g.command('vid_restart', 5.0)[-10:])))
    elif what == 'engine':
        for g in clients:
            out.append(relaunch(cfg, g, []))
    return '\n'.join(out)


def ui_load(game):
    """ui_load closes the menus: the ones open before are opened again (the focused one last, on its page), "main"
    when none was open and no level is loaded (the window stays black otherwise)."""
    try:
        before = game.dump()['menus']
    except RuntimeError:
        before = None
    out = game.command('ui_load', 2.0)[-10:]
    if before is None:
        return '\n'.join(out)
    shown = lambda ms: [(m['name'], m.get('page')) for m in ms]
    try:
        now = game.dump()['menus']
    except RuntimeError:
        return '\n'.join(out)
    if shown(now) == shown(before) and (now or before):
        return '\n'.join(out)
    menus = sorted(before, key=lambda m: m.get('focus') == '1')
    if not menus:
        try:
            game.level(0)
            return '\n'.join(out)
        except RuntimeError:
            menus = [{'name': 'main'}]
    cmds = []
    for i, m in enumerate(menus):
        cmds.append('ui_openmenu %s%s' % (m['name'], ' over' if i else ''))
        # (v_uiPage turns the focused menu: the one just opened)
        if m.get('page', '0') != '0':
            cmds.append('v_uiPage %s' % m['page'])
    game.send('\n'.join(cmds))
    time.sleep(0.3)
    out.append('open menus: ' + (', '.join(m['name'] for m in game.dump()['menus']) or 'none'))
    return '\n'.join(out)


def relaunch(cfg, game, keep):
    """The same client again with the same options, on its map at its place and view, with the cvars of keep."""
    opts = dict(game.opts)
    place = None
    values = {}
    if game.running():
        for name in keep or []:
            v = game.cvar(name)
            if v is not None:
                values[name] = v
        try:
            d = game.level(0)
            if d['time'].get('server', 0) > 0:
                place = d
                if not opts.get('map') and not opts.get('connect'):
                    # (the level names it maps/mp/ffa3.bsp, devmap wants mp/ffa3)
                    m = d['time'].get('map') or ''
                    if m.startswith('maps/'):
                        m = m[5:]
                    if m.endswith('.bsp'):
                        m = m[:-4]
                    opts['map'] = m
        except RuntimeError:
            pass
        game.stop()
    opts['cvars'] = dict(opts.get('cvars') or {}, **values)
    msg = [game.start(opts)]
    if place and (opts.get('map') or opts.get('connect')):
        wait_ingame(game, True, 90)
        p, ang = place['player']['origin'], place['player']['angles']
        msg.append(place_player(game, p, ang[1], ang[0]))
    if values:
        # (again in the game: a cgame may set its cvars when it loads)
        game.send('\n'.join('set %s "%s"' % kv for kv in values.items()))
        msg.append('kept ' + ', '.join('%s=%s' % kv for kv in values.items()))
    return '; '.join(msg)


def place_player(game, origin, yaw, pitch, timeout=3.0, still=True):
    """setviewpos (still: without the push of a teleport), and wait until the snapshots of the server show the player there."""
    lines = game.command('setviewpos %g %g %g %g %g%s' % (origin[0], origin[1], origin[2], yaw, pitch, ' still' if still else ''), 0.2)
    bad = [line for line in lines if 'usage' in line.lower() or 'cheat' in line.lower()]
    if bad:
        raise RuntimeError('setviewpos: ' + ' | '.join(bad) + ' (cheats: launch with map=, or sv_cheats 1 on the server)')
    end = time.time() + timeout
    while time.time() < end:
        o = game.level(0)['player']['origin']
        if all(abs(o[i] - origin[i]) < 4.0 for i in range(3)):
            return 'at %g %g %g, yaw %g pitch %g' % (origin[0], origin[1], origin[2], yaw, pitch)
        time.sleep(0.05)
    return 'sent the player to %g %g %g (not there after %gs: blocked, or falling)' % (origin[0], origin[1], origin[2], timeout)


MANIFEST = 'jkamcp_prepared.json'   # in game_dir: the files prepare made there, the only ones it removes


def prepare(cfg):
    if any(g.running() for g in GAMES.values()):
        raise RuntimeError('quit the clients and the server first')
    game_dir = os.path.abspath(cfg['game_dir'])
    not_temporary(game_dir, 'game_dir')
    other = foreign_processes(cwd=game_dir)
    if other:
        raise RuntimeError('a game of another session runs from %s (pid %d: %s): prepare would change its build; give '
                           'this session its own game_dir and home with configure' % ((game_dir,) + other[0]))
    p = cfg.get('prepare') or {}
    sources = [os.path.realpath(entry[0]) for entry in (p.get('link') or [])]
    for s in sources:
        if game_dir == s or game_dir.startswith(s + os.sep):
            raise RuntimeError('game_dir %s is inside the source folder %s: prepare never writes there' % (game_dir, s))
    out = []
    n = 0
    made = []       # what prepare puts into game_dir, relative: the files of an earlier prepare that are no longer wanted go
    for entry in p.get('link') or []:
        src, dst = entry[0], entry[1]
        pattern = entry[2] if len(entry) > 2 else '*'
        dst = os.path.join(game_dir, dst)
        os.makedirs(dst, exist_ok=True)
        for f in sorted(glob.glob(os.path.join(src, pattern))):
            link = os.path.join(dst, os.path.basename(f))
            made.append(os.path.relpath(link, game_dir))
            if os.path.islink(link) and os.path.realpath(link) == os.path.realpath(f):
                continue
            if os.path.lexists(link):
                os.remove(link)
            os.symlink(os.path.abspath(f), link)
            n += 1
    out.append('%d new links' % n)
    for src, dst in p.get('copy') or []:
        target = os.path.join(game_dir, dst)
        if os.path.realpath(src) == os.path.realpath(target):
            raise RuntimeError('prepare.copy: %s is its own destination' % src)
        put_file(src, target)
        # (the mode as the source has it: an executable stays one)
        os.chmod(target, os.stat(src).st_mode & 0o7777)
        made.append(os.path.normpath(dst))
        out.append('copied %s' % dst)
    manifest = os.path.join(game_dir, MANIFEST)
    old = []
    if os.path.exists(manifest):
        with open(manifest, encoding='utf-8') as f:
            old = json.load(f)
    gone = []
    for rel in old:
        path = os.path.join(game_dir, rel)
        # (only what an earlier prepare made, inside game_dir, and only links or files)
        if rel in made or os.path.isabs(rel) or rel.startswith('..'):
            continue
        if os.path.islink(path) or os.path.isfile(path):
            os.remove(path)
            gone.append(rel)
    with open(manifest, 'w', encoding='utf-8') as f:
        json.dump(sorted(set(made)), f, indent=1)
    if gone:
        out.append('removed %d from an earlier prepare: %s' % (len(gone), ', '.join(gone)))
    return 'prepared %s: %s' % (game_dir, ', '.join(out))


def config_text(cfg):
    """What configure shows: where the configuration comes from, what this session changed, the folders and the builds."""
    out = ['file %s%s' % (config_path(), ' (chosen by configure)' if OVERRIDES['file'] else '')]
    if OVERRIDES['replace']:
        out.append('replace ' + json.dumps(OVERRIDES['replace']))
    if OVERRIDES['set']:
        out.append('set ' + json.dumps(OVERRIDES['set']))
    for k in ('game_dir', 'home', 'server_port', 'renderer', 'launcher'):
        out.append('%s %s' % (k, cfg.get(k)))
    for src, dst in (cfg.get('prepare') or {}).get('copy') or []:
        out.append('prepare %s <- %s' % (dst, src))
    if (cfg.get('reload') or {}).get('cwd'):
        out.append('reload in %s' % cfg['reload']['cwd'])
    shared = load_config(own=False)
    if OVERRIDES['file'] or OVERRIDES['replace'] or OVERRIDES['set']:
        same = [k for k in ('game_dir', 'home') if shared.get(k) and os.path.realpath(shared[k]) == os.path.realpath(cfg.get(k) or '')]
        if same and json.dumps(shared.get('prepare'), sort_keys=True) != json.dumps(cfg.get('prepare'), sort_keys=True):
            out.append('WARNING: %s %s the same as in the shared configuration, but the build is another: prepare would put '
                       'this build into the folder other sessions test with; replace or set %s too'
                       % (' and '.join(same), 'are' if len(same) > 1 else 'is', ' and '.join(same)))
    return '\n'.join(out)


def call(cfg, name, a):
    """Returns (content, structuredContent or None)."""
    if name == 'launch':
        g = game_of(cfg, a, create=True)
        return [text(g.start(a))], None
    if name == 'quit':
        if a.get('all'):
            out = [g.stop() for g in GAMES.values() if g.proc]
            return [text('; '.join(out) or 'nothing was running')], None
        return [text(game_of(cfg, a).stop())], None
    if name == 'server_launch':
        g = game_of(cfg, a, create=True, server=True)
        opts = dict(a)
        opts['cvars'] = dict({'g_gametype': str(a.get('gametype', 0))}, **(a.get('cvars') or {}))
        return [text(g.start(opts))], None
    if name == 'server_quit':
        return [text(game_of(cfg, a, server=True).stop())], None
    if name == 'console':
        g = game_of(cfg, a, server=bool(a.get('server')))
        lines = g.command(a['command'], float(a.get('wait', 0.5)), a.get('until'), float(a.get('timeout', 10)))
        return [text('\n'.join(lines) or '(nothing printed)')], None
    if name == 'wait':
        g = game_of(cfg, a, server=bool(a.get('server')))
        kind, timeout = a['kind'], float(a.get('timeout', 60))
        if kind == 'ingame':
            d = wait_ingame(g, a.get('join', True), timeout)
            return [text('in the game: ' + '\n'.join(level_text(d).splitlines()[:3]))], None
        if kind == 'log':
            line = g.wait_for(a['pattern'], timeout)
            if line is None:
                raise RuntimeError('no line matched %r in %ds' % (a['pattern'], timeout))
            return [text(line)], None
        if kind == 'cvar':
            end = time.time() + timeout
            v = None
            while True:
                v = g.cvar(a['name'])
                if v == str(a.get('value')):
                    return [text('%s = %s' % (a['name'], v))], None
                if time.time() >= end:
                    break
                time.sleep(0.25)
            raise RuntimeError('%s is %r, not %r, after %ds' % (a['name'], v, a.get('value'), timeout))
        if kind == 'time':
            took = g.wait_game_ms(int(a.get('ms', 1000)), timeout)
            return [text('the game clock went on by %d ms' % took)], None
        raise RuntimeError('unknown kind %r' % kind)
    if name == 'screenshot':
        g = game_of(cfg, a)
        rect, p = framing(g, a)
        data = g.screenshot()
        sid = keep_shot(data, g.name)
        note = 'screenshot %s' % sid + (' (point at %d,%d)' % p if p else '')
        if rect:
            note += ', frame %d,%d %dx%d' % tuple(round(v) for v in rect)
        return [text(note), image_content(data, int(a.get('max_width', 1280)), int(a.get('quality', 85)), rect,
                                          float(a.get('zoom', 1)), fit=bool(a.get('fit')))], None
    if name == 'sequence':
        g = game_of(cfg, a)
        rect, _ = framing(g, a)
        at = sorted(int(t) for t in (a.get('at_ms') or a.get('times') or []))
        if not at:
            raise RuntimeError('at_ms is empty: give the game times, e.g. [0, 200, 400]')
        start = g.game_time()
        if a.get('hold'):
            press(g, a['hold'], True)
        shots = []
        try:
            for t in at:
                while True:
                    now = g.game_time()
                    if now - start >= t:
                        break
                    time.sleep(0.005)
                data = g.screenshot()
                shots.append((now - start, data))
        finally:
            if a.get('hold'):
                press(g, a['hold'], False)
        ids = [keep_shot(d, '%s +%dms' % (g.name, t)) for t, d in shots]
        ims = [picture(d, rect, float(a.get('zoom', 1))) for _, d in shots]
        if ims[0] is None:
            return [text('screenshots ' + ', '.join(ids) + ' (Pillow is missing: no picture)')], None
        im = strip(ims, ['%s +%d ms' % (sid, t) for sid, (t, _) in zip(ids, shots)])
        return [text('screenshots %s at %s ms of game time' % (', '.join(ids), ', '.join(str(t) for t, _ in shots))),
                image_content(None, int(a.get('max_width', 1600)), 85, im=im)], None
    if name == 'compare':
        pics = []
        for k in ('a', 'b'):
            if a[k] not in SHOTS:
                raise RuntimeError('no screenshot %r (there are %s)' % (a[k], ', '.join(SHOTS) or 'none'))
            pics.append(picture(SHOTS[a[k]][0], a.get('crop'), float(a.get('zoom', 1))))
        if pics[0] is None:
            raise RuntimeError('compare needs Pillow (pip install pillow)')
        im = strip(pics, ['%s %s' % (a[k], SHOTS[a[k]][1]) for k in ('a', 'b')])
        return [image_content(None, int(a.get('max_width', 1600)), 85, im=im)], None
    if name == 'state':
        g = game_of(cfg, a)
        d = g.level(a.get('radius', 1024))
        if a.get('format') == 'json':
            return [text(json.dumps(d))], d
        return [text(level_text(d))], None
    if name == 'project':
        g = game_of(cfg, a)
        res = []
        for p in a.get('points') or []:
            q = g.project(p)
            res.append(None if q is None else [round(q[0], 1), round(q[1], 1)])
        return [text(json.dumps(res))], {'pixels': res}
    if name == 'hold':
        g = game_of(cfg, a)
        ms = int(a.get('ms', 500))
        press(g, a['buttons'], True)
        try:
            took = g.wait_game_ms(ms)
        finally:
            press(g, a['buttons'], False)
        return after(g, a, 'held %s for %d ms of game time' % (' '.join(a['buttons']), took)), None
    if name == 'view':
        g = game_of(cfg, a)
        msg = []
        if a.get('origin'):
            yaw, pitch = a.get('yaw'), a.get('pitch')
            if yaw is None or pitch is None:
                # (what is not given stays as it is)
                ang = g.level(0)['player']['angles']
                yaw = ang[1] if yaw is None else yaw
                pitch = ang[0] if pitch is None else pitch
            msg.append(place_player(g, a['origin'], float(yaw), float(pitch), still=a.get('still', True)))
        cmds = []
        if a.get('third') is not None:
            cmds.append('cg_thirdPerson %d' % (1 if a['third'] else 0))
        if a.get('range') is not None:
            cmds.append('cg_thirdPersonRange %g' % a['range'])
        if a.get('angle') is not None:
            cmds.append('cg_thirdPersonAngle %g' % a['angle'])
        if cmds:
            g.send('\n'.join(cmds))
            msg.append(', '.join(cmds))
        return after(g, a, '; '.join(msg) or 'nothing to do'), None
    if name == 'audio':
        g = game_of(cfg, a)
        if not g.audio_path:
            raise RuntimeError('launch the client with audio=true first')
        # the format of the device, as the engine printed it when its sound started (SDL_AudioSpec)
        rate, fmt, channels = None, None, None
        for line in list(g.lines):
            m = re.search(r'Freq:\s+(\d+)', line)
            if m:
                rate = int(m.group(1))
            m = re.search(r'Format:\s+(\S+)', line)
            if m:
                fmt = m.group(1)
            m = re.search(r'Channels:\s+(\d+)', line)
            if m:
                channels = int(m.group(1))
        if rate is None or fmt is None or channels is None:
            raise RuntimeError('the engine printed no format of its sound device (SDL_AudioSpec) yet')
        if fmt != 'AUDIO_S16LSB' or channels != 2:
            raise RuntimeError('the sound device is %s with %d channels; the analysis reads AUDIO_S16LSB stereo' % (fmt, channels))
        start = os.path.getsize(g.audio_path) if os.path.exists(g.audio_path) else 0
        if a.get('hold'):
            press(g, a['hold'], True)
        try:
            time.sleep(float(a.get('seconds', 2)))
        finally:
            if a.get('hold'):
                press(g, a['hold'], False)
        with open(g.audio_path, 'rb') as f:
            f.seek(start)
            raw = f.read()
        rows, whole, peak = analyse_audio(raw, rate, int(a.get('window_ms', 100)))
        out = ['%.2f s at %d Hz: loudness %.1f dBFS, peak %.1f dBFS' % (len(raw) / 4.0 / rate, rate, whole, peak)]
        for t, db, top in rows:
            out.append('%6.2f s %6.1f dB  %s' % (t, db, ', '.join('%.0f Hz' % f for f in top) if top else '-'))
        return [text('\n'.join(out))], None
    if name == 'menu_dump':
        g = game_of(cfg, a)
        d = g.dump()
        flt = (a.get('filter') or '').lower()
        if a.get('format') == 'json':
            for menu in d['menus']:
                menu['items'] = [i for i in menu['items']
                                 if (a.get('decorations') or 'D' not in i.get('flags', ''))
                                 and (not flt or any(flt in (i.get(k) or '').lower() for k in ('name', 'text', 'cvar')))]
            return [text(json.dumps(d))], d
        return [text(concise(d, flt, a.get('decorations')))], None
    if name == 'mouse':
        g = game_of(cfg, a)
        action = '' if a.get('action') in (None, '', 'move') else a['action']
        g.send('v_uiMouse %s %s %s px' % (a['x'], a['y'], action))
        time.sleep(0.15)
        return after(g, a, 'pointer at %s,%s %s' % (a['x'], a['y'], action or 'moved')), None
    if name == 'input':
        g = game_of(cfg, a)
        done = []
        with XInput(g) as xi:
            if a.get('x') is not None or a.get('y') is not None:
                if a.get('x') is None or a.get('y') is None:
                    raise RuntimeError('give both x and y')
                px, py = pointer_to(g, xi, float(a['x']), float(a['y']), a.get('home', True), a.get('step_px'))
                done.append('pointer at %d,%d' % (px, py))
            action = a.get('action') or 'move'
            button = 3 if action == 'rightclick' else int(a.get('button', 1))
            if action in ('click', 'rightclick'):
                for _ in range(max(1, int(a.get('count', 1)))):
                    xi.button(button, True)
                    xi.button(button, False)
                done.append('clicked button %d' % button)
            elif action in ('hold', 'release'):
                xi.button(button, action == 'hold')
                done.append('%s button %d' % ('holding' if action == 'hold' else 'released', button))
            elif action in ('wheelup', 'wheeldown'):
                for _ in range(max(1, int(a.get('count', 1)))):
                    xi.button(4 if action == 'wheelup' else 5, True)
                    xi.button(4 if action == 'wheelup' else 5, False)
                done.append('%s x%d' % (action, max(1, int(a.get('count', 1)))))
            if a.get('key'):
                xi.key(a['key'])
                done.append('key %s' % a['key'])
            if a.get('text'):
                for ch in a['text']:
                    xi.key(ch)
                done.append('typed %s' % json.dumps(a['text']))
            display = xi.display
        time.sleep(0.1)
        return after(g, a, '%s (X display %s)' % ('; '.join(done) or 'nothing to do', display)), None
    if name == 'configure':
        if any(g.running() for g in GAMES.values()):
            raise RuntimeError('quit the clients and the server first (configure changes their folders)')
        if a.get('reset'):
            OVERRIDES.update({'file': None, 'replace': {}, 'set': {}})
        if a.get('file'):
            f = os.path.abspath(os.path.expanduser(a['file']))
            if not os.path.isfile(f):
                raise RuntimeError('no file %s' % f)
            OVERRIDES['file'] = f
        if a.get('replace') is not None:
            OVERRIDES['replace'] = {os.path.expanduser(k): os.path.expanduser(v) for k, v in (a['replace'] or {}).items()}
        for k, v in (a.get('set') or {}).items():
            if v is None:
                OVERRIDES['set'].pop(k, None)
            else:
                OVERRIDES['set'][k] = v
        fresh = load_config()
        cfg.clear()
        cfg.update(fresh)
        # (the games that do not run made their homes from the old configuration)
        for k in [k for k, g in GAMES.items() if not g.running()]:
            del GAMES[k]
        return [text(config_text(cfg))], None
    if name == 'click':
        g = game_of(cfg, a)
        menu, item = find_item(g, a['item'], a.get('menu'))
        x, y, w, h = item['rect']
        g.send('v_uiMouse %d %d click px' % (round(x + w / 2), round(y + h / 2)))
        time.sleep(0.2)
        return after(g, a, 'clicked %s %s of %s at %d,%d' % (item['name'], json.dumps(item['text']), menu['name'],
                                                              x + w / 2, y + h / 2)), None
    if name == 'key':
        g = game_of(cfg, a)
        sent = []
        if a.get('key'):
            k = a['key']
            num = KEYS.get(k.lower())
            if num is None and len(k) == 1:
                num = ord(k)
            if num is None:
                raise RuntimeError('unknown key %r; use one character or one of %s' % (k, ', '.join(sorted(KEYS))))
            g.send('v_uiKey %d' % num)
            sent.append(k)
        if a.get('text'):
            g.send('\n'.join('v_uiKey %d char' % ord(ch) for ch in a['text']))
            sent.append(json.dumps(a['text']))
        time.sleep(0.15)
        return after(g, a, 'pressed ' + ' '.join(sent)), None
    if name in ('reload', 'prepare'):
        # (the configuration read again: a changed jka-mcp.json counts without reconnecting the server; in place, the
        # clients keep a reference to it)
        fresh = load_config()
        cfg.clear()
        cfg.update(fresh)
    if name == 'reload':
        return [text(do_reload(cfg, a['what']))], None
    if name == 'relaunch':
        return [text(relaunch(cfg, game_of(cfg, a), a.get('keep') or []))], None
    if name == 'prepare':
        return [text(prepare(cfg))], None
    if name == 'log':
        g = game_of(cfg, a, server=bool(a.get('server')))
        since = a.get('since')
        body, cursor = g.tail(int(a.get('lines', 60)), int(since) if since is not None else None, a.get('pattern'))
        return [text('%s\n(cursor %d)' % (body or '(no lines)', cursor))], None
    raise KeyError(name)


# ---- the protocol

def main():
    cfg = load_config()
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
                content, structured = call(cfg, name, params.get('arguments') or {})
                result = {'content': content, 'isError': False}
                if structured is not None:
                    result['structuredContent'] = structured
                reply(msg_id, result)
            except Exception as e:
                reply(msg_id, {'content': [text('%s: %s' % (type(e).__name__, e))], 'isError': True})
        else:
            reply(msg_id, error={'code': -32601, 'message': 'unknown method %s' % method})
    for g in list(GAMES.values()):
        g.stop()


if __name__ == '__main__':
    main()
