#!/usr/bin/env python3
"""Cabin TUI: offline flight entertainment in a terminal.

Reads the catalogue the Cabin desktop app uses and opens films in mpv,
magazines in Evince, puzzles natively and Atari ST games in Hatari.
Python standard library only; nothing here touches the network.
"""
import argparse
import curses
import hashlib
import json
import locale
import os
from pathlib import Path
import shutil
import subprocess
import sys
import textwrap
import time

HOME = Path.home()
XDG_STATE = Path(os.environ.get('XDG_STATE_HOME', str(HOME / '.local/state')))
LIBRARY = Path(os.environ.get('CABIN_LIBRARY', str(HOME / '.local/share/cabin/library.json')))
HISTORY = XDG_STATE / 'cabin' / 'history.json'      # shared with the desktop app
STATE = XDG_STATE / 'cabin-tui' / 'state.json'      # ours alone
ATARI_STATE = XDG_STATE / 'atari-games'             # writable disk copies, shared
WATCH_LATER = [XDG_STATE / 'mpv' / 'watch_later', HOME / '.config/mpv/watch_later']
ATARI_ROOT = HOME / 'games/atari-st'

MPV, EVINCE, HATARI = '/usr/bin/mpv', '/usr/bin/evince', '/usr/bin/hatari'
PLAYERS = {'watch': MPV, 'listen': MPV, 'read': EVINCE, 'atari': HATARI}

# Hardware decoding saves battery; levelled audio and subtitles help over
# engine noise. Quitting mpv normally stores the position.
MPV_ARGS = ['--save-position-on-quit', '--hwdec=auto-safe', '--af=dynaudnorm',
            '--sub-auto=fuzzy', '--slang=en,eng']

SECTIONS = [('home', 'Home'), ('watch', 'Watch'), ('read', 'Read'), ('puzzles', 'Puzzles'),
            ('atari', 'Atari'), ('favorites', 'Favorites'), ('recent', 'Recent'), ('plan', 'Plan')]

HELP = [
    ('Left / Right, Tab', 'Change section'),
    ('Up / Down, PgUp / PgDn', 'Move through the list'),
    ('Enter', 'Watch, read or play'),
    ('/', 'Search; Enter keeps it, Esc clears it'),
    ('t', 'Cycle topic filter'),
    ('f', 'Save or remove a favourite'),
    ('w', 'Mark a film watched or unwatched'),
    ('p', 'Plan a line-up for the flight time'),
    ('u', 'Show or hide untested Atari games'),
    ('d / J', 'Atari: next boot disk / joystick on arrows'),
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
            read = lambda name: (bat / name).read_text().strip()
            percent, status, hours = int(read('capacity')), read('status'), None
            try:
                power = int(read('power_now'))
                if status == 'Discharging' and power > 0:
                    hours = int(read('energy_now')) / power
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
        for source, dest in zip(sources, disks):
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
        return [MPV, '--save-position-on-quit', '--no-video', '--', path], None
    if kind == 'read':
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


# ----------------------------------------------------------------------- UI

class App:
    def __init__(self, items):
        self.items = items
        history = load_json(HISTORY, {})
        self.favorites = set(history.get('favorites', []))
        self.recent = list(history.get('recent', []))
        state = load_json(STATE, {})
        self.watched = set(state.get('watched', []))
        self.show_untested = bool(state.get('show_untested', False))
        self.section, self.selected, self.top = 0, 0, 0
        self.query, self.topic, self.searching = '', '', False
        self.disk_index, self.joystick = 0, False
        self.plan_minutes, self.plan_rows, self.plan_used = 0, [], 0
        self.message, self.help = '', False
        self.refresh_progress()

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
            return [item for item, _ in self.plan_rows]
        rows = [i for i in self.items if self.show_untested or i.get('kind') != 'atari' or i.get('verified')]
        if key == 'home' and self.query:
            pass                                    # a search from Home covers everything
        elif key == 'home':
            # Films you started, then unwatched films, then things to read or hear.
            started = [i for i in rows if self.progress.get(i['id'], (None,))[0]]
            rest = [i for i in rows if i not in started and i['id'] not in self.watched]
            rows = started + [i for kind in ('watch', 'read', 'listen') for i in rest if i.get('kind') == kind]
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
        """Short right-hand column: progress for films, size for the rest."""
        if item.get('kind') == 'watch' and item.get('seconds'):
            position, watched = self.progress.get(item['id'], (None, False))
            if watched:
                return 'watched'
            if position:
                return f'{minutes(max(0, item["seconds"] - position))} left'
        return str(item.get('meta', ''))

    # -- actions

    def launch(self, item):
        try:
            cmd, work = command_for(item, self.disk_index, self.joystick)
            if not Path(cmd[0]).is_file():
                raise FileNotFoundError(f'Not installed: {cmd[0]}')
            log_path = (work or STATE.parent) / ('hatari.log' if work else 'launch.log')
            log_path.parent.mkdir(parents=True, exist_ok=True)
            curses.def_prog_mode()
            curses.endwin()
            print(f'\n  {item["title"]} is open. Close it to come back to Cabin.\n', flush=True)
            try:
                with log_path.open('a') as log:
                    code = subprocess.run(cmd, cwd=work, stdout=log, stderr=log).returncode
            finally:
                curses.reset_prog_mode()
            self.recent = [item['id']] + [r for r in self.recent if r != item['id']]
            if item.get('kind') == 'watch':
                # mpv keeps a resume file unless the film ran to its end.
                if resume_position(item['path']) is None and code == 0:
                    self.watched.add(item['id'])
                else:
                    self.watched.discard(item['id'])
                self.refresh_progress()
            self.message = f'Closed {item["title"]}.' if code == 0 else f'{item["title"]} exited with an error; see {log_path}'
            self.save()
        except (OSError, ValueError) as exc:
            self.message = str(exc)

    def make_plan(self, total):
        self.plan_minutes = total
        self.plan_rows, self.plan_used = plan(self.items, total, self.progress, self.favorites)
        self.section, self.selected = [k for k, _ in SECTIONS].index('plan'), 0

    # -- drawing

    def draw(self, scr):
        h, w = scr.getmaxyx()
        scr.erase()
        if h < 12 or w < 50:
            scr.addnstr(0, 0, 'Cabin needs a larger terminal (50 x 12).', max(1, w - 1))
            scr.refresh()
            return
        accent, dim = curses.color_pair(1), curses.color_pair(2)

        def put(y, x, text, style=0, width=None):
            room = w - x - 1 if width is None else min(width, w - x - 1)
            if 0 <= y < h and room > 0:
                try:
                    scr.addnstr(y, x, text, room, style)
                except curses.error:
                    pass

        # Header: name on the left, clock and battery on the right.
        put(0, 1, 'CABIN', accent | curses.A_BOLD)
        put(0, 8, 'offline flight entertainment', dim)
        right = time.strftime('%H:%M')
        bat = battery()
        if bat:
            percent, status, hours = bat
            right += f'   battery {percent}%'
            if hours:
                right += f', {minutes(hours * 3600)} left'
            elif status == 'Charging':
                right += ', charging'
        put(0, max(40, w - len(right) - 2), right, dim)

        x = 1
        for n, (_, label) in enumerate(SECTIONS):
            style = accent | curses.A_REVERSE | curses.A_BOLD if n == self.section else 0
            put(2, x, f' {label} ', style)
            x += len(label) + 3

        rows = self.visible()
        self.selected = max(0, min(self.selected, len(rows) - 1))
        key = SECTIONS[self.section][0]

        # Filter line.
        bits = []
        if self.searching or self.query:
            bits.append('Search: ' + self.query + ('_' if self.searching else ''))
        if self.topic:
            bits.append('Topic: ' + self.topic)
        if key == 'plan':
            bits.append(f'{minutes(self.plan_used)} of films for a {minutes(self.plan_minutes * 60)} flight'
                        if self.plan_minutes else 'Press p and enter the flight time')
        elif key == 'atari' and self.hidden_untested():
            bits.append(f'{self.hidden_untested()} untested games hidden (u shows them)')
        bits.append(f'{len(rows)} item' + ('' if len(rows) == 1 else 's'))
        put(3, 1, '   '.join(bits), dim)

        # List on the left, detail on the right.
        list_w = max(30, min(60, w * 45 // 100))
        body = h - 7
        if self.selected < self.top:
            self.top = self.selected
        if self.selected >= self.top + body:
            self.top = self.selected - body + 1
        self.top = max(0, min(self.top, max(0, len(rows) - body)))
        for n, item in enumerate(rows[self.top:self.top + body]):
            index, y = self.top + n, 5 + n
            mark = '*' if item['id'] in self.favorites else ' '
            status = self.status(item)
            title_w = list_w - len(status) - 5
            title = item['title'] if len(item['title']) <= title_w else item['title'][:title_w - 1] + '…'
            line = f' {mark} {title:<{title_w}} {status} '
            put(y, 1, line, curses.A_REVERSE if index == self.selected else 0, list_w)
        if not rows:
            empty = {'favorites': 'Press f on anything to keep it here.',
                     'recent': 'What you open will appear here.',
                     'plan': 'Nothing fits yet. Press p to plan a flight.'}.get(key, 'Nothing matches.')
            put(6, 3, empty, dim)

        if rows:
            self.draw_detail(put, rows[self.selected], list_w + 3, w - list_w - 5, body, accent, dim)

        hint = ('Type to search   Enter: keep   Esc: clear' if self.searching else
                'Enter: open   /: search   f: favourite   p: plan   t: topic   ?: help   q: quit')
        put(h - 2, 1, self.message, accent)
        put(h - 1, 1, hint, dim)
        if self.help:
            self.draw_help(scr, h, w, accent)
        scr.refresh()

    def draw_detail(self, put, item, x, width, height, accent, dim):
        if width < 20:
            return
        y = 5
        put(y, x, str(item.get('tag', '')).upper(), accent)
        put(y + 1, x, item['title'], curses.A_BOLD, width)
        put(y + 2, x, str(item.get('subtitle', '')), dim, width)
        y += 4
        kind = item.get('kind')
        facts = [str(item.get('meta', ''))]
        if item.get('bytes'):
            facts.append(f'{item["bytes"] / 1e6:.0f} MB')
        put(y, x, '  ·  '.join(f for f in facts if f), 0, width)
        y += 1
        if kind == 'watch' and item.get('seconds'):
            position, watched = self.progress.get(item['id'], (None, False))
            fraction = 1 if watched else min(1, (position or 0) / item['seconds'])
            bar_w = min(30, width - 12)
            filled = int(round(bar_w * fraction))
            put(y, x, '#' * filled + '-' * (bar_w - filled), accent)
            put(y, x + bar_w + 1, 'watched' if watched else f'{int(fraction * 100)}%', dim)
            y += 1
        y += 1
        for line in textwrap.wrap(str(item.get('description', '')), width)[:max(0, height - 14)]:
            put(y, x, line, 0, width)
            y += 1
        y += 1
        notes = []
        if kind == 'atari':
            disks = item.get('disks', [])
            if disks:
                notes.append(f'Boot disk: {Path(disks[self.disk_index % len(disks)]).name} ({len(disks)} disk(s), d changes it)')
            notes.append('Joystick on arrow keys + right Ctrl: ' + ('on' if self.joystick else 'off') + ' (J toggles)')
            if not item.get('verified'):
                notes.append('Untested: this copy has not been boot-checked.')
            notes += textwrap.wrap(str(item.get('notes', '')), width)
            notes.append('In Hatari: F11 fullscreen, F12 settings, Right Alt+M frees the mouse.')
        elif kind == 'watch':
            notes.append('Opens fullscreen in mpv. Space pauses, arrows seek, q quits and keeps your place.')
        elif kind == 'read':
            notes.append('Opens in Document Viewer.')
        elif kind == 'puzzles':
            notes.append('A new puzzle every time; rules are in its Help menu.')
        for note in notes:
            for line in textwrap.wrap(note, width) or ['']:
                if y < 5 + height:
                    put(y, x, line, dim, width)
                    y += 1

    def draw_help(self, scr, h, w, accent):
        box_w, box_h = min(w - 4, 72), min(h - 2, len(HELP) + 4)
        top, left = (h - box_h) // 2, (w - box_w) // 2
        win = scr.derwin(box_h, box_w, top, left)
        win.erase()
        win.box()
        win.addnstr(1, 2, 'Keys', box_w - 4, accent | curses.A_BOLD)
        for n, (keys, what) in enumerate(HELP[:box_h - 4]):
            win.addnstr(3 + n, 2, f'{keys:<26}{what}', box_w - 4)

    # -- input

    def prompt(self, scr, label):
        h, w = scr.getmaxyx()
        scr.move(h - 2, 0)
        scr.clrtoeol()
        scr.addnstr(h - 2, 1, label, w - 2)
        curses.echo()
        curses.curs_set(1)
        try:
            return scr.getstr(h - 2, len(label) + 1, 12).decode(errors='replace')
        finally:
            curses.noecho()
            curses.curs_set(0)

    def on_key(self, scr, key):
        """Handle one key. Returns False to quit."""
        self.message = ''
        rows = self.visible()
        item = rows[self.selected] if rows else None
        if self.help:
            self.help = False
            return True
        if self.searching:
            if key in ('\n', '\r', curses.KEY_ENTER):
                self.searching = False
            elif key == '\x1b':
                self.searching, self.query = False, ''
            elif key in ('\x7f', '\b', curses.KEY_BACKSPACE):
                self.query = self.query[:-1]
            elif isinstance(key, str) and key.isprintable():
                self.query += key
            self.selected = 0
            return True
        if key in ('q', 'Q'):
            return False
        page = max(1, scr.getmaxyx()[0] - 8)
        if key in (curses.KEY_UP, 'k'):
            self.selected -= 1
        elif key in (curses.KEY_DOWN, 'j'):
            self.selected += 1
        elif key == curses.KEY_PPAGE:
            self.selected -= page
        elif key == curses.KEY_NPAGE:
            self.selected += page
        elif key in (curses.KEY_HOME, 'g'):
            self.selected = 0
        elif key in (curses.KEY_END, 'G'):
            self.selected = len(rows) - 1
        elif key in (curses.KEY_RIGHT, 'l', '\t'):
            self.section, self.selected = (self.section + 1) % len(SECTIONS), 0
        elif key in (curses.KEY_LEFT, 'h', curses.KEY_BTAB):
            self.section, self.selected = (self.section - 1) % len(SECTIONS), 0
        elif key == '/':
            self.searching, self.query, self.selected = True, '', 0
        elif key == '\x1b':
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
                self.refresh_progress()
                self.message = f'Reloaded: {len(self.items)} items.'
            except (OSError, ValueError) as exc:
                self.message = f'Could not reload: {exc}'
        elif key == 'p':
            total = parse_duration(self.prompt(scr, 'Flight time (3:10, 3h10 or 190): '))
            if total and total > 0:
                self.make_plan(total)
            else:
                self.message = 'That is not a flight time.'
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
        elif item and key in ('\n', '\r', curses.KEY_ENTER):
            self.launch(item)
            scr.clear()
        if key in (curses.KEY_UP, curses.KEY_DOWN, 'j', 'k', curses.KEY_PPAGE, curses.KEY_NPAGE,
                   curses.KEY_HOME, curses.KEY_END, 'g', 'G'):
            self.disk_index = 0
        return True

    def run(self, scr):
        curses.curs_set(0)
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_CYAN, -1)
        curses.init_pair(2, 8 if curses.COLORS > 8 else curses.COLOR_WHITE, -1)
        curses.set_escdelay(25)
        scr.keypad(True)
        scr.timeout(30000)          # redraw the clock and battery now and then
        while True:
            self.draw(scr)
            try:
                key = scr.get_wch()
            except curses.error:
                continue
            except KeyboardInterrupt:
                return
            if key == curses.KEY_RESIZE:
                continue
            if not self.on_key(scr, key):
                return


def main(argv=None):
    parser = argparse.ArgumentParser(prog='cabin-tui', description=__doc__.splitlines()[0])
    parser.add_argument('--list', action='store_true', help='list the catalogue')
    parser.add_argument('--check', action='store_true', help='check players and files before a flight')
    parser.add_argument('--plan', metavar='TIME', help='print a line-up for a flight, e.g. 3:10')
    parser.add_argument('--dry-run', metavar='ID', help='print the command an item would run')
    args = parser.parse_args(argv)
    try:
        items = load_library()
    except (OSError, ValueError) as exc:
        raise SystemExit(f'Cannot read the catalogue {LIBRARY}: {exc}')
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
        total = parse_duration(args.plan)
        if not total or total <= 0:
            parser.error('TIME looks like 3:10, 3h10 or 190')
        app = App(items)
        rows, used = plan(items, total, app.progress, app.favorites)
        for item, left in rows:
            print(f'{minutes(left):>12}  {item["title"]}')
        print(f'{minutes(used):>12}  of films for a {minutes(total * 60)} flight')
    elif args.dry_run:
        item = next((i for i in items if i['id'] == args.dry_run), None)
        if not item:
            raise SystemExit('Unknown item id')
        try:
            print(json.dumps(command_for(item, prepare=False)[0]))
        except (OSError, ValueError) as exc:
            raise SystemExit(str(exc))
    elif not (sys.stdin.isatty() and sys.stdout.isatty()):
        parser.error('Run cabin-tui in a terminal')
    else:
        locale.setlocale(locale.LC_ALL, '')
        curses.wrapper(App(items).run)


if __name__ == '__main__':
    main()
