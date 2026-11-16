#!/usr/bin/env python3
"""Cabin TUI: offline flight entertainment in a terminal.

A big-screen view: artwork, large titles and a shelf of cards. It reads a JSON
catalogue (the one the Cabin desktop app uses) and opens films in mpv,
magazines in Evince, puzzles natively and Atari ST games in Hatari.
Python standard library only; nothing here touches the network.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import select
import shutil
import signal
import struct
import subprocess
import sys
import termios
import textwrap
import threading
import time
import tty

__version__ = '0.3.4'

HOME = Path.home()
XDG_STATE = Path(os.environ.get('XDG_STATE_HOME', str(HOME / '.local/state')))
LIBRARY = Path(os.environ.get('CABIN_LIBRARY', str(HOME / '.local/share/cabin/library.json')))
HISTORY = XDG_STATE / 'cabin' / 'history.json'      # shared with the desktop app
STATE = XDG_STATE / 'cabin-tui' / 'state.json'      # ours alone
ATARI_STATE = XDG_STATE / 'atari-games'             # writable disk copies, shared
WATCH_LATER = [XDG_STATE / 'mpv' / 'watch_later', HOME / '.config/mpv/watch_later']
ATARI_ROOT = HOME / 'games/atari-st'

MPV, EVINCE, HATARI = '/usr/bin/mpv', '/usr/bin/evince', '/usr/bin/hatari'
MAGICK, FOOT, HYPRCTL = '/usr/bin/magick', '/usr/bin/foot', '/usr/bin/hyprctl'
XDG_OPEN = '/usr/bin/xdg-open'      # for books saved as web pages
CABIN = str(HOME / '.local/bin/cabin')   # the desktop app, whose reader suits scanned magazines
CACHE = Path(os.environ.get('XDG_CACHE_HOME', str(HOME / '.cache'))) / 'cabin-tui'   # prepared pictures
TITLE_FONT = '/usr/share/fonts/liberation/LiberationSans-Bold.ttf'   # falls back to fontconfig's bold sans
MEMORY_PICTURES = 96      # prepared pictures kept in memory; the rest are re-read from the disk cache
PLAYERS = {'watch': MPV, 'listen': MPV, 'read': EVINCE, 'atari': HATARI}

# Hardware decoding saves battery; levelled audio and subtitles help over
# engine noise. Quitting mpv normally stores the position.
MPV_ARGS = ['--save-position-on-quit', '--hwdec=auto-safe', '--af=dynaudnorm',
            '--sub-auto=fuzzy', '--slang=en,eng']

SECTIONS = [('home', 'Home'), ('watch', 'Watch'), ('listen', 'Listen'), ('read', 'Read'), ('puzzles', 'Puzzles'),
            ('atari', 'Atari'), ('favorites', 'Favorites'), ('recent', 'Recent'), ('plan', 'Plan')]

HELP = [
    ('Left / Right', 'Browse the shelf'),
    ('Up / Down, Tab', 'Change section'),
    ('PgUp / PgDn, Home / End', 'Jump along the shelf'),
    ('Enter', 'Watch, read or play'),
    ('/', 'Search; Enter keeps it, Esc clears it'),
    ('t', 'Cycle topic filter'),
    ('f', 'Save or remove a favourite'),
    ('w', 'Mark a film watched or unwatched'),
    ('p', 'Plan a line-up for the flight time'),
    ('u', 'Show or hide untested Atari games'),
    ('d / J', 'Atari: next boot disk / arrow-key joystick'),
    ('r', 'Reload the catalogue'),
    ('q', 'Quit'),
]


# ------------------------------------------------------------------ data

def load_json(path, fallback):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return fallback


def save_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def load_library(path=None):
    items = json.loads(Path(path or LIBRARY).read_text())
    if not isinstance(items, list):
        raise ValueError('The catalogue is not a list of items')
    return [i for i in items if isinstance(i, dict) and i.get('id') and i.get('title')]


def resume_position(path, dirs=None):
    """Seconds into a film where mpv will resume, or None if it has no saved place."""
    name = hashlib.md5(str(path).encode()).hexdigest().upper()
    for folder in dirs or WATCH_LATER:
        try:
            for line in (Path(folder) / name).read_text(errors='replace').splitlines():
                if line.startswith('start='):
                    return float(line[6:])
        except (OSError, ValueError):
            continue
    return None


def minutes(seconds):
    m = int(round(seconds / 60))
    return f'{m // 60} h {m % 60:02d} min' if m >= 60 else f'{m} min'


def battery():
    """(percent, status, hours left or None), or None on a machine without one."""
    for bat in sorted(Path('/sys/class/power_supply').glob('BAT*')):
        try:
            percent, status, hours = int((bat / 'capacity').read_text()), (bat / 'status').read_text().strip(), None
            try:
                power = int((bat / 'power_now').read_text())
                if status == 'Discharging' and power > 0:
                    hours = int((bat / 'energy_now').read_text()) / power
            except (OSError, ValueError):
                pass
            return percent, status, hours
        except (OSError, ValueError):
            continue
    return None


def matches(item, query):
    hay = ' '.join(str(item.get(k, '')) for k in ('title', 'subtitle', 'tag', 'description', 'meta')).casefold()
    return all(word in hay for word in query.casefold().split())


# --------------------------------------------------------------- launching

def atari_command(item, disk_index=0, joystick=False, prepare=True):
    folder = item.get('folder', '')
    if not folder or '/' in folder or folder == '..':
        raise ValueError('Invalid game folder')
    rom = Path(item.get('rom', ''))
    if not rom.is_file():
        raise FileNotFoundError(f'EmuTOS ROM missing: {rom}')
    sources = [Path(d) for d in item.get('disks', [])]
    if folder == 'space-crusade-voyage-beyond':
        # The expansion boots from the base game's disk.
        sources.insert(0, ATARI_ROOT / 'space-crusade/051.MSA')
        disk_index = 0
    if not sources:
        raise ValueError('No disk images are listed for this game')
    for source in sources:
        if not source.is_file():
            raise FileNotFoundError(f'Missing disk: {source}')
    work = ATARI_STATE / folder
    disks = [work / s.name for s in sources]
    config, empty = work / 'launcher.cfg', work / 'native.keymap'
    if prepare:
        work.mkdir(parents=True, exist_ok=True)
        # Play on persistent copies so saves never alter the archived images.
        for source, dest in zip(sources, disks, strict=True):
            if not dest.exists():
                shutil.copy2(source, dest)
        if not config.exists():
            config.write_text('# Isolated launcher configuration\n')
        # An explicit empty mapping stops a global key map leaking into other games.
        empty.touch(exist_ok=True)
    disk_index %= len(disks)
    mapping = empty
    arrows = HOME / '.local/share/laser-squad-st/arrows.keymap'
    if folder == 'laser-squad' and arrows.is_file():
        mapping = arrows
    elif folder == 'sabre-team':
        mapping = ATARI_ROOT / 'launcher/sabre-team.keymap'
    second = next((d for i, d in enumerate(disks) if i != disk_index), 'none')
    cmd = [HATARI, '--configfile', str(config), '--tos', str(rom), '--machine', 'st',
           '--memsize', '1', '--monitor', 'rgb', '--fullscreen', '--zoom', '2',
           '--resizable', 'on', '--grab', '--confirm-quit', 'on',
           '--disk-a', str(disks[disk_index]), '--disk-b', str(second),
           '--protect-floppy', 'off', '--joy0', 'none',
           '--joy1', 'keys' if joystick else 'none', '--keymap', str(mapping)]
    return cmd, work


def command_for(item, disk_index=0, joystick=False, prepare=True):
    """(argument list, working directory or None). Paths are never passed through a shell."""
    kind, path = item.get('kind'), str(item.get('path', ''))
    if kind == 'atari':
        return atari_command(item, disk_index, joystick, prepare)
    if not Path(path).is_file():
        raise FileNotFoundError(f'The local file is missing: {path}')
    if kind == 'watch':
        return [MPV, *MPV_ARGS, '--fs', '--', path], None
    if kind == 'listen':
        # A small window gives the audio its controls; q there keeps the place.
        return [MPV, '--no-video', '--force-window=yes', '--save-position-on-quit', '--af=dynaudnorm', '--', path], None
    if kind == 'read':
        if path.lower().endswith(('.html', '.htm')):
            return [XDG_OPEN, path], None
        # The desktop app's reader shows a page at a readable width; Evince shows scans tiny.
        if Path(CABIN).is_file() and item.get('id'):
            return [CABIN, '--read', str(item['id'])], None
        return [EVINCE, path], None
    if kind == 'puzzles':
        name = path.removeprefix('/usr/bin/puzzles-')
        if name == path or not name or '/' in name:
            raise ValueError('Invalid puzzle executable')
        return [path], None
    raise ValueError(f'Unsupported item type: {kind}')


def check(items):
    """Problems that would stop something opening in the air."""
    problems = []
    for kind, exe in PLAYERS.items():
        if any(i.get('kind') == kind for i in items) and not Path(exe).is_file():
            problems.append(f'Player not installed: {exe}')
    folders = set()
    for item in items:
        try:
            cmd, _ = command_for(item, prepare=False)
            if item.get('kind') != 'atari':
                path = Path(item['path'])
                folders.add(path.parent)
                if path.stat().st_size == 0:
                    problems.append(f'{item["title"]}: file is empty: {path}')
            else:
                for disk in item.get('disks', []):
                    if Path(disk).stat().st_size == 0:
                        problems.append(f'{item["title"]}: disk is empty: {disk}')
            if not Path(cmd[0]).is_file():
                problems.append(f'{item["title"]}: not installed: {cmd[0]}')
        except (OSError, ValueError) as exc:
            problems.append(f'{item["title"]}: {exc}')
    for folder in sorted(f for f in folders if f != Path('/usr/bin')):
        for part in sorted(folder.glob('*.part')):
            problems.append(f'Unfinished download: {part}')
    return sorted(set(problems))


# ------------------------------------------------------------------ planner

def plan(items, total_minutes, progress, favorites=()):
    """Films that fit the flight: started ones first, then favourites, then the rest.

    `progress` maps item id to (position seconds or None, watched bool).
    Returns (chosen [(item, seconds still to watch)], seconds used).
    """
    budget, candidates = total_minutes * 60, []
    for order, item in enumerate(items):
        if item.get('kind') != 'watch' or not item.get('seconds'):
            continue
        position, watched = progress.get(item['id'], (None, False))
        if watched:
            continue
        left = max(60, item['seconds'] - (position or 0))
        rank = 0 if position else 1 if item['id'] in favorites else 2
        candidates.append((rank, order, item, left))
    chosen, used = [], 0
    for _, _, item, left in sorted(candidates, key=lambda c: c[:2]):
        if used + left <= budget:
            chosen.append((item, left))
            used += left
    return chosen, used


def plan_legs(items, legs, progress, favorites=()):
    """A line-up for a trip of several flights: no film is split across two legs.

    `legs` is minutes per leg. Returns (rows [(item, seconds to watch, leg number)],
    seconds used per leg).
    """
    rows, used, taken = [], [], set()
    for number, leg in enumerate(legs, 1):
        chosen, seconds = plan([i for i in items if i['id'] not in taken], leg, progress, favorites)
        rows += [(item, left, number) for item, left in chosen]
        taken |= {item['id'] for item, _ in chosen}
        used.append(seconds)
    return rows, used


def parse_trip(text):
    """'3:40, stop 1:15, 1:25' -> ([220, 75, 85], ['flight', 'stop', 'flight']); None if unclear.

    A part that starts with 'stop', 'wait' or 's' is time on the ground.
    """
    legs, kinds = [], []
    for part in (p.strip().lower() for p in re.split(r'[,+]', text)):
        if not part:
            continue
        ground = re.match(r'(stopover|stop|wait|s)\s*', part)
        legs.append(parse_duration(part[ground.end():] if ground else part))
        kinds.append('stop' if ground else 'flight')
    return (legs, kinds) if legs and all(leg and leg > 0 for leg in legs) else None


def segment_names(kinds):
    """['flight', 'stop', 'flight'] -> ['Flight 1', 'Stopover', 'Flight 2']."""
    names, flights = [], 0
    for kind in kinds:
        if kind == 'stop':
            names.append('Stopover')
        else:
            flights += 1
            names.append(f'Flight {flights}')
    return names


def parse_legs(text):
    """'4:20, 2:05' or '4h20 + 2h05' -> [260, 125]; None if any part makes no sense."""
    legs = [parse_duration(part) for part in re.split(r'[,+]', text) if part.strip()]
    return legs if legs and all(leg and leg > 0 for leg in legs) else None


def parse_duration(text):
    """'3:10', '3h10', '3h' or '190' -> minutes; None if it makes no sense."""
    text = text.strip().lower().replace(' ', '')
    try:
        for sep in (':', 'h'):
            if sep in text:
                hours, _, mins = text.partition(sep)
                return int(hours or 0) * 60 + int(mins.rstrip('m') or 0)
        return int(text.rstrip('m'))
    except ValueError:
        return None


# ------------------------------------------------------------------ artwork

def caption_text(text):
    """Make a title safe for ImageMagick's caption: (no file reads, no % escapes)."""
    text = text.replace('\\', '\\\\').replace('%', '%%')
    return '\\' + text if text.startswith('@') else text


def title_font():
    """A bold sans font file for the large titles, or None to leave the choice to ImageMagick."""
    if Path(TITLE_FONT).is_file():
        return TITLE_FONT
    try:
        found = subprocess.run(['fc-match', '-f', '%{file}', 'sans:bold'], capture_output=True, text=True, timeout=5)
        return found.stdout.strip() if found.returncode == 0 and Path(found.stdout.strip()).is_file() else None
    except (OSError, subprocess.SubprocessError):
        return None


class Art:
    """Turns artwork and titles into sixel images with ImageMagick, off the UI thread.

    Results are cached on disk, so each picture is prepared once per size.
    """

    def __init__(self, wake, enabled=True):
        self.enabled = enabled and Path(MAGICK).is_file()
        self.wake, self.ready, self.pending, self.failed = wake, {}, set(), set()
        self.jobs, self.font = queue.Queue(), title_font()
        if self.enabled:
            for _ in range(3):
                threading.Thread(target=self.work, daemon=True).start()

    def get(self, mode, source, w, h, bg='#000000', fg='#ffffff', size=0):
        """Sixel bytes, or None while the picture is being prepared (or cannot be)."""
        if not self.enabled or not source or w < 8 or h < 8:
            return None
        stamp = 0
        if mode != 'title':
            try:
                stamp = Path(source).stat().st_mtime_ns
            except OSError:
                return None
        key = hashlib.sha1(repr((mode, source, stamp, w, h, bg, fg, size)).encode()).hexdigest()
        if key in self.ready:
            self.ready[key] = self.ready.pop(key)       # most recently used goes last
            return self.ready[key]
        if key in self.failed:
            return None
        cached = CACHE / (key + '.six')
        try:
            self.keep(key, cached.read_bytes())
            return self.ready[key]
        except OSError:
            pass
        if key not in self.pending:
            self.pending.add(key)
            self.jobs.put((key, mode, source, w, h, bg, fg, size))
        return None

    def keep(self, key, data):
        self.ready[key] = data
        while len(self.ready) > MEMORY_PICTURES:
            self.ready.pop(next(iter(self.ready)), None)

    def work(self):
        while True:
            key, mode, source, w, h, bg, fg, size = self.jobs.get()
            box, data, stdin = f'{w}x{h}', None, None
            if mode == 'title':
                font = ['-font', self.font] if self.font else []
                cmd = [MAGICK, '-background', bg, '-fill', fg, *font, '-pointsize', str(size),
                       '-size', f'{w}x', 'caption:' + caption_text(source),
                       '-gravity', 'NorthWest', '-extent', box, 'sixel:-']
            else:
                # 'cover' fills the box and crops the overflow; 'top' does the same but keeps
                # the top of the picture (a magazine's masthead); 'contain' shows all of it.
                fit = ['-resize', box, '-background', bg] if mode == 'contain' else ['-resize', box + '^']
                gravity = 'North' if mode == 'top' else 'center'
                # The picture goes in on stdin, so its file name is never parsed by ImageMagick.
                cmd = [MAGICK, '-[0]', *fit, '-gravity', gravity, '-extent', box, 'sixel:-']
            try:
                if mode != 'title':
                    stdin = open(source, 'rb')
                out = subprocess.run(cmd, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30)
                if out.returncode == 0 and out.stdout.startswith(b'\x1bP'):
                    data = out.stdout
                    CACHE.mkdir(parents=True, exist_ok=True)
                    tmp = CACHE / (key + '.tmp')
                    tmp.write_bytes(data)
                    tmp.replace(CACHE / (key + '.six'))
            except (OSError, subprocess.SubprocessError):
                pass
            finally:
                if stdin:
                    stdin.close()
            if data:
                self.keep(key, data)
            else:
                self.failed.add(key)
            self.pending.discard(key)
            try:
                os.write(self.wake, b'x')
            except OSError:
                pass


# -------------------------------------------------------------------- theme

DEFAULT_THEME = {'background': '#12151c', 'darker_background': '#0b0d12', 'lighter_background': '#1e2430',
                 'selection': '#2a3142', 'foreground': '#c8cede', 'bright_foreground': '#eef1f7',
                 'dark_foreground': '#7c869c', 'accent': '#7aa2f7', 'green': '#9ece6a', 'yellow': '#e0af68'}


def load_theme(path=None):
    """Colours from the current Omarchy theme, with defaults for anything missing."""
    colours = dict(DEFAULT_THEME)
    try:
        text = Path(path or HOME / '.local/state/omarchy/current/theme/colors.toml').read_text()
    except OSError:
        text = ''
    for line in text.splitlines():
        match = re.match(r'\s*([a-z_]+)\s*=\s*"(#[0-9a-fA-F]{6})"', line)
        if match and match.group(1) in colours:
            colours[match.group(1)] = match.group(2).lower()
    return colours


def rgb(colour):
    return int(colour[1:3], 16), int(colour[3:5], 16), int(colour[5:7], 16)


def fit(text, width):
    """Text cut to a width, with an ellipsis when something was lost."""
    text = str(text)
    if width <= 0:
        return ''
    return text if len(text) <= width else text[:width - 1] + '…'


KEYS = {'[A': 'up', '[B': 'down', '[C': 'right', '[D': 'left', 'OA': 'up', 'OB': 'down', 'OC': 'right',
        'OD': 'left', '[5~': 'pgup', '[6~': 'pgdn', '[H': 'home', '[F': 'end', '[1~': 'home', '[4~': 'end',
        'OH': 'home', 'OF': 'end', '[Z': 'btab'}
SEQUENCE = re.compile(r'\x1b(\[[0-9;?]*[A-Za-z~]|O[A-Z])')


def parse_keys(text):
    """Split terminal input into key names ('left', 'enter', 'esc') and typed characters."""
    keys, i = [], 0
    while i < len(text):
        char = text[i]
        if char == '\x1b':
            match = SEQUENCE.match(text, i)
            if match:
                if match.group(1) in KEYS:
                    keys.append(KEYS[match.group(1)])
                i = match.end()
                continue
            keys.append('esc')
        elif char in '\r\n':
            keys.append('enter')
        elif char == '\t':
            keys.append('tab')
        elif char in '\x7f\b':
            keys.append('backspace')
        elif char.isprintable():
            keys.append(char)
        i += 1
    return keys


# ----------------------------------------------------------------------- UI

ACTIONS = {'watch': 'WATCH NOW', 'listen': 'LISTEN NOW', 'read': 'READ NOW'}


class App:
    def __init__(self, items, art=True):
        self.items = items
        history = load_json(HISTORY, {})
        self.favorites = set(history.get('favorites', []))
        self.recent = list(history.get('recent', []))
        state = load_json(STATE, {})
        self.watched = set(state.get('watched', []))
        self.show_untested = bool(state.get('show_untested', False))
        self.section, self.selected, self.top = 0, 0, 0
        self.query, self.topic, self.mode, self.entry = '', '', '', ''
        self.disk_index, self.joystick = 0, False
        self.legs, self.kinds, self.plan_rows, self.plan_used = [], [], [], []
        self.message, self.help = '', False
        self.want_art, self.art, self.theme = art, None, load_theme()
        self.refresh_progress()
        # The plan lives in the history file, so the desktop app shows the same trip.
        saved = history.get('plan') or state.get('plan', [])
        if isinstance(saved, list) and saved and all(isinstance(leg, int) and leg > 0 for leg in saved):
            self.legs = saved
            kinds = history.get('plan_kinds', [])
            self.kinds = kinds if isinstance(kinds, list) and len(kinds) == len(saved) else ['flight'] * len(saved)
            self.plan_rows, self.plan_used = plan_legs(self.items, saved, self.progress, self.favorites)

    # -- state

    def refresh_progress(self):
        self.progress = {}
        for item in self.items:
            if item.get('kind') == 'watch':
                position = resume_position(item.get('path', ''))
                if position is not None:
                    self.watched.discard(item['id'])
                self.progress[item['id']] = (position, item['id'] in self.watched)

    def save(self):
        try:
            history = load_json(HISTORY, {})
            history['favorites'] = sorted(self.favorites)
            history['recent'] = self.recent[:24]
            history['plan'] = self.legs
            history['plan_kinds'] = self.kinds
            save_json(HISTORY, history)
            save_json(STATE, {'watched': sorted(self.watched), 'show_untested': self.show_untested})
        except OSError as exc:
            self.message = f'Could not save: {exc}'

    def topics(self):
        return sorted({i.get('tag', '') for i in self.items if i.get('kind') in ('watch', 'read', 'listen')} - {''})

    def hidden_untested(self):
        return 0 if self.show_untested else sum(
            1 for i in self.items if i.get('kind') == 'atari' and not i.get('verified'))

    def visible(self):
        key = SECTIONS[self.section][0]
        if key == 'plan':
            return [item for item, _, _ in self.plan_rows]
        rows = [i for i in self.items if self.show_untested or i.get('kind') != 'atari' or i.get('verified')]
        if key == 'home' and self.query:
            pass                                    # a search from Home covers everything
        elif key == 'home':
            # Films you started, then unwatched films, then things to read. Audio has its own shelf.
            started = [i for i in rows if self.progress.get(i['id'], (None,))[0]]
            rest = [i for i in rows if i not in started and i['id'] not in self.watched]
            rows = started + [i for kind in ('watch', 'read') for i in rest if i.get('kind') == kind]
        elif key == 'favorites':
            rows = [i for i in rows if i['id'] in self.favorites]
        elif key == 'recent':
            order = {id_: n for n, id_ in enumerate(self.recent)}
            rows = sorted((i for i in rows if i['id'] in order), key=lambda i: order[i['id']])
        else:
            rows = [i for i in rows if i.get('kind') == key]
        if self.topic:
            rows = [i for i in rows if i.get('tag') == self.topic]
        if self.query:
            rows = [i for i in rows if matches(i, self.query)]
        return rows

    def status(self, item):
        """Short second line of a card: progress for films, size for the rest."""
        if SECTIONS[self.section][0] == 'plan' and len(self.legs) > 1:
            for planned, left, leg in self.plan_rows:
                if planned['id'] == item['id']:
                    return f'{segment_names(self.kinds)[leg - 1]} · {round(left / 60)} min'
        if item.get('kind') == 'watch' and item.get('seconds'):
            position, watched = self.progress.get(item['id'], (None, False))
            if watched:
                return 'Watched'
            if position:
                return f'{minutes(max(0, item["seconds"] - position))} left'
        return str(item.get('meta', ''))

    # -- actions

    def launch(self, item, out=None):
        try:
            cmd, work = command_for(item, self.disk_index, self.joystick)
            if not Path(cmd[0]).is_file():
                raise FileNotFoundError(f'Not installed: {cmd[0]}')
            log_path = (work or STATE.parent) / ('hatari.log' if work else 'launch.log')
            log_path.parent.mkdir(parents=True, exist_ok=True)
            if out:
                self.message = f'{item["title"]} is open. Close it to come back to Cabin.'
                self.draw(out)
            with log_path.open('a') as log:
                code = subprocess.run(cmd, cwd=work, stdin=subprocess.DEVNULL, stdout=log, stderr=log).returncode
            if out:
                # Keys pressed while the other app was open are not meant for Cabin.
                termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
                restore_window()
            self.recent = [item['id']] + [r for r in self.recent if r != item['id']]
            if item.get('kind') == 'watch':
                # mpv keeps a resume file unless the film ran to its end.
                if resume_position(item['path']) is None and code == 0:
                    self.watched.add(item['id'])
                else:
                    self.watched.discard(item['id'])
                self.refresh_progress()
            handed_over = cmd[0] == XDG_OPEN        # the browser keeps running after xdg-open returns
            self.message = f'Opened {item["title"]} in your browser.' if handed_over and code == 0 else \
                f'Closed {item["title"]}.' if code == 0 else f'{item["title"]} exited with an error; see {log_path}'
            self.save()
        except (OSError, ValueError) as exc:
            self.message = str(exc)

    def make_plan(self, legs, kinds=None):
        self.legs = list(legs)
        self.kinds = list(kinds) if kinds and len(kinds) == len(legs) else ['flight'] * len(legs)
        self.plan_rows, self.plan_used = plan_legs(self.items, self.legs, self.progress, self.favorites)
        self.section, self.selected = [k for k, _ in SECTIONS].index('plan'), 0
        self.save()

    # -- drawing

    def geometry(self):
        """(columns, rows, cell width px, cell height px) of the terminal."""
        try:
            rows, cols, xpx, ypx = struct.unpack('HHHH', fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, b'\0' * 8))
        except OSError:
            rows, cols, xpx, ypx = 24, 80, 0, 0
        if not (xpx and ypx and cols and rows):
            return cols or 80, rows or 24, 10, 20
        return cols, rows, xpx // cols, ypx // rows

    def draw(self, out):
        W, H, cw, ch = self.geometry()
        t = self.theme
        bg, deep, card, sel = t['background'], t['darker_background'], t['lighter_background'], t['selection']
        text, bright, dim, accent = t['foreground'], t['bright_foreground'], t['dark_foreground'], t['accent']
        buf, images = [], []

        def colour(fg=None, back=None, bold=False):
            parts = ['0']
            if bold:
                parts.append('1')
            if fg:
                parts.append('38;2;%d;%d;%d' % rgb(fg))
            parts.append('48;2;%d;%d;%d' % rgb(back or bg))
            return '\x1b[' + ';'.join(parts) + 'm'

        def put(y, x, string, fg=text, back=None, bold=False, width=None):
            room = min(W - x, width if width is not None else W)
            if 0 <= y < H and x >= 0 and room > 0:
                buf.append(f'\x1b[{y + 1};{x + 1}H{colour(fg, back, bold)}{str(string)[:room]}')

        def fill(y, x, w, h, back):
            for row in range(y, y + h):
                put(row, x, ' ' * w, None, back)

        def picture(y, x, cols, rows, mode, source, back, **more):
            """Queue a picture for cell box (cols x rows); True when it is ready."""
            if not self.art or self.help:
                return False
            data = self.art.get(mode, source, cols * cw, rows * ch, bg=back, **more)
            if data:
                images.append((y, x, data))
            return bool(data)

        buf.append('\x1b[?2026h' + colour(text) + '\x1b[2J')
        if W < 80 or H < 24:
            put(1, 2, 'Cabin needs more room.', bright, bold=True)
            put(2, 2, 'At least 80 x 24.', dim)
            out.write(''.join(buf) + '\x1b[?2026l')
            out.flush()
            return

        margin = 3
        rows = self.visible()
        self.selected = max(0, min(self.selected, len(rows) - 1))
        item = rows[self.selected] if rows else None
        key, label = SECTIONS[self.section]

        # Top bar: name, sections, clock and battery.
        put(1, margin, '✈  CABIN', accent, bold=True)
        tabs_w = sum(len(name) + 4 for _, name in SECTIONS)
        x = max(margin + 12, (W - tabs_w) // 2)
        for n, (_, name) in enumerate(SECTIONS):
            if n == self.section:
                put(1, x, f'  {name}  ', deep, accent, bold=True)
            else:
                put(1, x, f'  {name}  ', dim)
            x += len(name) + 4
        right = time.strftime('%H:%M')
        bat = battery()
        if bat:
            percent, status, hours = bat
            right = (f'{minutes(hours * 3600)} left  ' if hours else 'charging  ' if status == 'Charging' else '') \
                + f'{percent}%   ' + right
        put(1, W - margin - len(right), right, dim)

        # Shelf geometry first; the hero takes what is left above it.
        card_w = max(18, min(30, (W - 2 * margin) // 5 - 2))
        thumb_rows = max(3, round(card_w * cw * 9 / 16 / ch))
        card_h = thumb_rows + 2
        shelf_y = H - 2 - card_h
        hero_y, hero_h = 3, shelf_y - 6

        # Hero: artwork on the left, everything about the item on the right.
        if item and hero_h >= 8:
            kind = item.get('kind')
            art_cols = min(int(W * 0.46), int(hero_h * ch * 16 / 9 / cw))
            art_rows = min(hero_h, max(4, int(art_cols * cw * 9 / 16 / ch)))
            fill(hero_y, margin, art_cols, art_rows, deep)
            source = item.get('image', '')
            # Covers are shown whole; everything else fills the frame.
            hero_mode = 'contain' if kind in ('read', 'listen') else 'cover'
            if not picture(hero_y, margin, art_cols, art_rows, hero_mode, source, deep):
                put(hero_y + art_rows // 2, margin + max(0, (art_cols - len(item['title'])) // 2),
                    fit(item['title'], art_cols), dim, deep)
            ix = margin + art_cols + 3
            iw = W - ix - margin
            y = hero_y
            put(y, ix, fit(str(item.get('tag', '')).upper(), iw), accent, bold=True)
            y += 2
            # The title is drawn as a picture so it can be far larger than terminal text.
            width_px = iw * cw
            size = int(ch * 1.5)
            if len(item['title']) * size * 0.55 > width_px * 2:
                size = int(ch * 1.05)
            # Two rows hold one line of that size; longer titles get four.
            title_rows = 2 if len(item['title']) * size * 0.6 < width_px else 4
            if not picture(y, ix, iw, title_rows, 'title', item['title'], bg, fg=bright, size=size):
                for n, line in enumerate(textwrap.wrap(item['title'], iw)[:2]):
                    put(y + n, ix, line, bright, bold=True)
            y += title_rows + 1
            put(y, ix, fit(item.get('subtitle', ''), iw), text)
            y += 2
            facts = [str(item.get('meta', ''))]
            if item.get('bytes'):
                facts.append(f'{item["bytes"] / 1e6:.0f} MB')
            if item['id'] in self.favorites:
                facts.append('★ Favourite')
            if kind == 'atari' and not item.get('verified'):
                facts.append('Untested')
            put(y, ix, fit('   ·   '.join(f for f in facts if f), iw), dim)
            y += 1
            if kind == 'watch' and item.get('seconds'):
                position, watched = self.progress.get(item['id'], (None, False))
                fraction = 1 if watched else min(1, (position or 0) / item['seconds'])
                if fraction:
                    bar_w = min(40, iw - 10)
                    filled = max(1, int(round(bar_w * fraction)))
                    y += 1
                    put(y, ix, '━' * filled, accent)
                    put(y, ix + filled, '━' * (bar_w - filled), sel)
                    put(y, ix + bar_w + 2, 'Watched' if watched else self.status(item), dim)
                    y += 1
            y += 1
            notes = []
            if kind == 'atari':
                disks = item.get('disks', [])
                if len(disks) > 1:
                    notes.append(f'Boot disk {Path(disks[self.disk_index % len(disks)]).name} of {len(disks)}  (d changes it)')
                notes.append('Joystick on arrow keys + right Ctrl: ' + ('on' if self.joystick else 'off') + '  (J)')
                notes += [str(item.get('notes', ''))] if item.get('notes') else []
                notes.append('In Hatari: F12 settings, Right Alt+M frees the mouse.')
            elif kind in ('watch', 'listen'):
                notes.append('Space pauses, arrows seek, q quits and keeps your place.')
            bottom = hero_y + hero_h
            note_lines = [line for note in notes for line in textwrap.wrap(note, iw)]
            room = bottom - y - 3 - len(note_lines)
            for line in textwrap.wrap(str(item.get('description', '')), min(iw, 76))[:max(0, room)]:
                put(y, ix, line, text)
                y += 1
            y += 1
            if y < bottom:
                action = ACTIONS.get(kind, 'PLAY NOW')
                put(y, ix, f'   ▶  {action}   ', deep, accent, bold=True)
                put(y, ix + len(action) + 12, 'Enter', dim)
                y += 2
            for line in note_lines:
                if y < bottom:
                    put(y, ix, line, dim)
                    y += 1
        elif not item:
            empty = {'favorites': 'Press f on anything to keep it here.',
                     'recent': 'What you open will appear here.',
                     'plan': 'Press p and enter your time in the air to get a line-up that fits.'}.get(key, 'Nothing matches.')
            put(hero_y + 3, margin, empty, bright, bold=True)

        # Shelf heading with whatever is filtering it.
        bits = [label.upper()]
        if self.query or self.mode == 'search':
            typing = self.mode == 'search'
            bits.append('Search: ' + (self.entry if typing else self.query) + ('▏' if typing else ''))
        if self.topic:
            bits.append('Topic: ' + self.topic)
        if key == 'plan':
            for number, (leg, used) in enumerate(zip(self.legs, self.plan_used, strict=False), 1):
                name = f'{segment_names(self.kinds)[number - 1]}: ' if len(self.legs) > 1 else ''
                bits.append(f'{name}{minutes(used)} of films for {minutes(leg * 60)}')
        if key == 'atari' and self.hidden_untested():
            bits.append(f'{self.hidden_untested()} untested hidden (u)')
        put(shelf_y - 2, margin, fit('   ·   '.join(bits), W - 2 * margin - 12), bright, bold=True)
        if rows:
            count = f'{self.selected + 1} / {len(rows)}'
            put(shelf_y - 2, W - margin - len(count), count, dim)

        # Shelf: a row of cards, scrolled so the chosen one is on screen.
        n_cards = max(1, (W - 2 * margin + 2) // (card_w + 2))
        if self.selected < self.top:
            self.top = self.selected
        if self.selected >= self.top + n_cards:
            self.top = self.selected - n_cards + 1
        self.top = max(0, min(self.top, max(0, len(rows) - n_cards)))
        x0 = max(margin, (W - (n_cards * (card_w + 2) - 2)) // 2)
        for n, entry in enumerate(rows[self.top:self.top + n_cards]):
            x, chosen = x0 + n * (card_w + 2), self.top + n == self.selected
            back, fg = (accent, deep) if chosen else (card, text)
            if chosen:
                put(shelf_y - 1, x, '▄' * card_w, accent)
            fill(shelf_y, x, card_w, thumb_rows, deep)
            mode = {'read': 'top', 'listen': 'contain'}.get(entry.get('kind'), 'cover')
            if not picture(shelf_y, x, card_w, thumb_rows, mode, entry.get('image', ''), deep):
                put(shelf_y + thumb_rows // 2, x + 1, fit(entry['title'], card_w - 2), dim, deep)
            star = '★ ' if entry['id'] in self.favorites else ''
            put(shelf_y + thumb_rows, x, ' ' + fit(star + entry['title'], card_w - 2).ljust(card_w - 1), fg, back, bold=True)
            put(shelf_y + thumb_rows + 1, x, ' ' + fit(self.status(entry), card_w - 2).ljust(card_w - 1),
                deep if chosen else dim, back)
        if self.top > 0:
            put(shelf_y + thumb_rows // 2, max(0, x0 - 2), '‹', accent, bold=True)
        if self.top + n_cards < len(rows):
            put(shelf_y + thumb_rows // 2, min(W - 1, x0 + n_cards * (card_w + 2) - 1), '›', accent, bold=True)

        # Footer: one line for a prompt or message, else the keys.
        if self.mode == 'plan':
            ask = 'Time per flight, and stops (3:10 or 3:40, stop 1:15, 1:25): '
            put(H - 1, margin, ask + self.entry + '▏', bright, bold=True)
        elif self.mode == 'search':
            put(H - 1, margin, 'Type to search     Enter keep     Esc clear', dim)
        elif self.message:
            put(H - 1, margin, fit(self.message, W - 2 * margin), accent, bold=True)
        else:
            put(H - 1, margin, fit('←  → browse     ↑  ↓ section     Enter open     / search     f favourite     '
                                   'p plan     ? help     q quit', W - 2 * margin), dim)

        if self.help:
            box_w, box_h = min(W - 8, 74), len(HELP) + 4
            top, left = max(1, (H - box_h) // 2), (W - box_w) // 2
            fill(top, left, box_w, box_h, card)
            put(top + 1, left + 3, 'KEYS', accent, card, bold=True)
            for n, (keys, what) in enumerate(HELP):
                put(top + 3 + n, left + 3, f'{keys:<26}', bright, card, bold=True)
                put(top + 3 + n, left + 30, fit(what, box_w - 33), text, card)

        out.write(''.join(buf))
        for y, x, data in images:
            out.write(f'\x1b[{y + 1};{x + 1}H')
            out.flush()
            out.buffer.write(data)
            out.buffer.flush()
        out.write('\x1b[0m\x1b[?2026l')
        out.flush()

    # -- input

    def on_key(self, key, out=None):
        """Handle one key. Returns False to quit."""
        self.message = ''
        rows = self.visible()
        item = rows[self.selected] if rows else None
        if self.help:
            self.help = False
            return True
        if self.mode:
            if key == 'enter':
                mode, self.mode = self.mode, ''
                if mode == 'search':
                    self.query = self.entry
                else:
                    trip = parse_trip(self.entry)
                    if trip:
                        self.make_plan(*trip)
                    else:
                        self.message = 'That is not a flight time.'
            elif key == 'esc':
                if self.mode == 'search':
                    self.query = ''
                self.mode = ''
            elif key == 'backspace':
                self.entry = self.entry[:-1]
            elif len(key) == 1:
                self.entry += key
            if self.mode == 'search':
                self.query, self.selected = self.entry, 0
            return True
        if key in ('q', 'Q'):
            return False
        page = 5
        moved = True
        if key in ('left', 'h'):
            self.selected -= 1
        elif key in ('right', 'l'):
            self.selected += 1
        elif key == 'pgup':
            self.selected -= page
        elif key == 'pgdn':
            self.selected += page
        elif key in ('home', 'g'):
            self.selected = 0
        elif key in ('end', 'G'):
            self.selected = len(rows) - 1
        elif key in ('down', 'j', 'tab'):
            self.section, self.selected, self.top = (self.section + 1) % len(SECTIONS), 0, 0
        elif key in ('up', 'k', 'btab'):
            self.section, self.selected, self.top = (self.section - 1) % len(SECTIONS), 0, 0
        else:
            moved = False
        if moved:
            self.disk_index = 0
        elif key == '/':
            self.mode, self.entry, self.query, self.selected = 'search', '', '', 0
        elif key == 'p':
            self.mode, self.entry = 'plan', ''
        elif key == 'esc':
            self.query, self.topic, self.selected = '', '', 0
        elif key == 't':
            options = [''] + self.topics()
            self.topic = options[(options.index(self.topic) + 1) % len(options)] if self.topic in options else ''
            self.selected = 0
        elif key == '?':
            self.help = True
        elif key == 'u':
            self.show_untested = not self.show_untested
            self.save()
        elif key == 'r':
            try:
                self.items = load_library()
                self.theme = load_theme()
                self.refresh_progress()
                self.message = f'Reloaded: {len(self.items)} items.'
            except (OSError, ValueError) as exc:
                self.message = f'Could not reload: {exc}'
        elif key == 'J':
            self.joystick = not self.joystick
        elif key in ('d', 'D'):
            self.disk_index += 1
        elif item and key == 'f':
            self.favorites ^= {item['id']}
            self.save()
        elif item and key == 'w' and item.get('kind') == 'watch':
            if self.progress.get(item['id'], (None,))[0]:
                self.message = 'This film has a saved place; finish it in mpv or keep watching.'
            else:
                self.watched ^= {item['id']}
                self.refresh_progress()
                self.save()
        elif item and key == 'enter':
            self.launch(item, out)
        return True

    def run(self):
        fd, out = sys.stdin.fileno(), sys.stdout
        wake_r, wake_w = os.pipe()
        os.set_blocking(wake_w, False)
        saved = termios.tcgetattr(fd)

        def resized(*_):
            try:
                os.write(wake_w, b'x')
            except OSError:
                pass

        signal.signal(signal.SIGWINCH, resized)
        try:
            tty.setcbreak(fd)
            out.write('\x1b[?1049h\x1b[?25l\x1b[c')      # own screen, no cursor, ask what the terminal can do
            out.flush()
            # Terminals that can draw sixel pictures say so with a 4 in their reply.
            reply = os.read(fd, 256).decode(errors='replace') if select.select([fd], [], [], 0.4)[0] else ''
            sixel = bool(re.search(r'\x1b\[\?[0-9;]*\b4\b[0-9;]*c', reply))
            self.art = Art(wake_w, enabled=self.want_art and sixel)
            pending = parse_keys(SEQUENCE.sub('', reply))
            while True:
                for key in pending:
                    if not self.on_key(key, out):
                        return
                self.draw(out)
                ready = select.select([fd, wake_r], [], [], 30)[0]    # the timeout keeps the clock fresh
                pending = []
                if wake_r in ready:
                    os.read(wake_r, 4096)
                if fd in ready:
                    pending = parse_keys(os.read(fd, 1024).decode(errors='replace'))
        except KeyboardInterrupt:
            pass
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)
            out.write('\x1b[0m\x1b[?25h\x1b[?1049l')
            out.flush()


def restore_window():
    """Give Cabin's own window focus and fullscreen back after another app closes.

    Hyprland takes a window out of fullscreen when another opens beside it. Only
    our own window is addressed, and only when Cabin opened it (--window).
    """
    if not os.environ.get('CABIN_TUI_WINDOW') or not Path(HYPRCTL).is_file():
        return
    try:
        listed = subprocess.run([HYPRCTL, 'clients', '-j'], capture_output=True, timeout=3)
        ours = [c for c in json.loads(listed.stdout) if c.get('class') == 'cabin-tui' and c.get('pid') == os.getppid()]
        if not ours:
            return
        window = 'window="address:%s"' % re.sub(r'[^0-9a-fx]', '', str(ours[0].get('address', '')))
        for call in (f'hl.dsp.focus({{{window}}})',
                     f'hl.dsp.window.fullscreen({{mode="fullscreen",action="set",{window}}})'):
            subprocess.run([HYPRCTL, 'dispatch', call], capture_output=True, timeout=3)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass


def open_window():
    """Reopen Cabin in its own fullscreen terminal with large type."""
    if not Path(FOOT).is_file():
        raise SystemExit('The big-screen window needs the foot terminal; run cabin-tui in any terminal instead.')
    size = os.environ.get('CABIN_TUI_FONT_SIZE', '15')
    os.environ['CABIN_TUI_WINDOW'] = '1'
    os.execv(FOOT, [FOOT, '--app-id=cabin-tui', '--title=Cabin', '--fullscreen',
                    f'--font=monospace:size={size}', '--override=pad=0x0',
                    sys.executable, str(Path(__file__).resolve())])


def main(argv=None):
    parser = argparse.ArgumentParser(prog='cabin-tui', description=__doc__.splitlines()[0])
    parser.add_argument('--version', action='version', version=f'cabin-tui {__version__}')
    parser.add_argument('--window', action='store_true', help='open in its own fullscreen window with large type')
    parser.add_argument('--no-art', action='store_true', help='text only, no pictures')
    parser.add_argument('--list', action='store_true', help='list the catalogue')
    parser.add_argument('--check', action='store_true', help='check players and files before a flight')
    parser.add_argument('--plan', metavar='TIME',
                        help='print a line-up, e.g. 3:10, or "3:40, stop 1:15, 1:25" for a trip with a stopover')
    parser.add_argument('--save-plan', action='store_true', help='with --plan: keep it, so the Plan section opens with it')
    parser.add_argument('--dry-run', metavar='ID', help='print the command an item would run')
    args = parser.parse_args(argv)
    if args.window:
        open_window()
    try:
        items = load_library()
    except (OSError, ValueError) as exc:
        raise SystemExit(f'Cannot read the catalogue {LIBRARY}: {exc}') from None
    if args.list:
        for item in items:
            print(f'{item["id"]:<34} {item.get("kind", ""):<8} {item["title"]}')
    elif args.check:
        problems = check(items)
        for problem in problems:
            print(problem)
        untested = sum(1 for i in items if i.get('kind') == 'atari' and not i.get('verified'))
        print(f'{len(items)} items, {len(problems)} problem(s), {untested} Atari games not boot-tested')
        raise SystemExit(1 if problems else 0)
    elif args.plan:
        trip = parse_trip(args.plan)
        legs, kinds = trip or (None, None)
        if not legs:
            parser.error('TIME looks like 3:10, 3h10 or 190; separate several flights with commas')
        app = App(items)
        rows, used = plan_legs(items, legs, app.progress, app.favorites)
        for number, (leg, seconds) in enumerate(zip(legs, used, strict=True), 1):
            print(f'{segment_names(kinds)[number - 1]}: {minutes(seconds)} of films for {minutes(leg * 60)}')
            for item, left, row_leg in rows:
                if row_leg == number:
                    print(f'{minutes(left):>12}  {item["title"]}')
        if args.save_plan:
            app.legs, app.kinds = legs, kinds
            app.save()
    elif args.dry_run:
        item = next((i for i in items if i['id'] == args.dry_run), None)
        if not item:
            raise SystemExit('Unknown item id')
        try:
            print(json.dumps(command_for(item, prepare=False)[0]))
        except (OSError, ValueError) as exc:
            raise SystemExit(str(exc)) from None
    elif not (sys.stdin.isatty() and sys.stdout.isatty()):
        parser.error('Run cabin-tui in a terminal')
    else:
        App(items, art=not args.no_art).run()


if __name__ == '__main__':
    main()
