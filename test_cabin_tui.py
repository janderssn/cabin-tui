import hashlib
import tempfile
import unittest
from pathlib import Path

import cabin_tui as c


def film(id_, seconds, **extra):
    return {'id': id_, 'title': id_, 'kind': 'watch', 'seconds': seconds, **extra}


class Planner(unittest.TestCase):
    def test_fits_the_flight_and_skips_watched(self):
        items = [film('a', 3000), film('b', 3000), film('c', 1500), film('seen', 600)]
        rows, used = c.plan(items, 80, {'seen': (None, True)})
        self.assertEqual([i['id'] for i, _ in rows], ['a', 'c'])
        self.assertEqual(used, 4500)

    def test_started_films_come_first_and_count_only_what_is_left(self):
        items = [film('new', 1200), film('started', 3600)]
        rows, used = c.plan(items, 30, {'started': (2400.0, False)})
        self.assertEqual([(i['id'], left) for i, left in rows], [('started', 1200)])
        self.assertEqual(used, 1200)

    def test_favourites_before_the_rest(self):
        rows, _ = c.plan([film('x', 600), film('fav', 600)], 10, {}, favorites={'fav'})
        self.assertEqual([i['id'] for i, _ in rows], ['fav'])

    def test_durations(self):
        for text, want in [('3:10', 190), ('3h10', 190), ('3h', 180), ('190', 190), ('45m', 45), ('soon', None)]:
            self.assertEqual(c.parse_duration(text), want, text)


class Commands(unittest.TestCase):
    def test_film_path_is_one_argument_after_a_separator(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'odd $(false) name.mkv'
            path.write_bytes(b'x')
            cmd, cwd = c.command_for({'kind': 'watch', 'path': str(path)})
        self.assertEqual(cmd[0], c.MPV)
        self.assertEqual(cmd[-2:], ['--', str(path)])
        self.assertIn('--hwdec=auto-safe', cmd)
        self.assertIsNone(cwd)

    def test_missing_file_is_an_error(self):
        with self.assertRaises(FileNotFoundError):
            c.command_for({'kind': 'read', 'path': '/definitely/missing.pdf'})

    def test_books_saved_as_web_pages_open_in_the_browser(self):
        with tempfile.TemporaryDirectory() as d:
            page, pdf = Path(d) / 'book.HTML', Path(d) / 'issue.pdf'
            page.write_text('x')
            pdf.write_bytes(b'x')
            self.assertEqual(c.command_for({'kind': 'read', 'path': str(page)})[0], [c.XDG_OPEN, str(page)])
            self.assertEqual(c.command_for({'kind': 'read', 'path': str(pdf)})[0], [c.EVINCE, str(pdf)])

    def test_only_puzzle_binaries_are_run(self):
        with self.assertRaises(ValueError):
            c.command_for({'kind': 'puzzles', 'path': '/usr/bin/sh'})

    def test_atari_folder_cannot_escape(self):
        with self.assertRaises(ValueError):
            c.atari_command({'kind': 'atari', 'folder': '../x', 'rom': '/x', 'disks': []}, prepare=False)

    def test_atari_boot_disk_and_joystick(self):
        with tempfile.TemporaryDirectory() as d:
            rom, a, b = (Path(d) / n for n in ('tos.img', 'A.MSA', 'B.MSA'))
            for f in (rom, a, b):
                f.write_bytes(b'x')
            item = {'kind': 'atari', 'folder': 'demo', 'rom': str(rom), 'disks': [str(a), str(b)]}
            cmd, work = c.atari_command(item, disk_index=1, joystick=True, prepare=False)
        self.assertEqual(cmd[cmd.index('--disk-a') + 1], str(work / 'B.MSA'))
        self.assertEqual(cmd[cmd.index('--disk-b') + 1], str(work / 'A.MSA'))
        self.assertEqual(cmd[cmd.index('--joy1') + 1], 'keys')


class Progress(unittest.TestCase):
    def test_reads_the_place_mpv_saved(self):
        with tempfile.TemporaryDirectory() as d:
            name = hashlib.md5(b'/films/a.mkv').hexdigest().upper()
            (Path(d) / name).write_text('start=754.321000\nvolume=80\n')
            self.assertAlmostEqual(c.resume_position('/films/a.mkv', [d]), 754.321)
            self.assertIsNone(c.resume_position('/films/other.mkv', [d]))

    def test_search_needs_every_word(self):
        item = {'title': 'Apollo 13', 'tag': 'Space'}
        self.assertTrue(c.matches(item, 'SPACE apollo'))
        self.assertFalse(c.matches(item, 'apollo vietnam'))

    def test_check_flags_empty_files_and_part_downloads(self):
        with tempfile.TemporaryDirectory() as d:
            empty = Path(d) / 'empty.pdf'
            empty.write_bytes(b'')
            (Path(d) / 'half.mkv.part').write_bytes(b'x')
            problems = c.check([{'id': 'm', 'title': 'Mag', 'kind': 'read', 'path': str(empty)}])
        self.assertTrue(any('empty' in p for p in problems))
        self.assertTrue(any('Unfinished download' in p for p in problems))


class Terminal(unittest.TestCase):
    def test_keys_are_split_from_escape_sequences(self):
        self.assertEqual(c.parse_keys('\x1b[C\x1bOBa\r\x1b[5~\x1b'), ['right', 'down', 'a', 'enter', 'pgup', 'esc'])

    def test_terminal_replies_are_not_typed_as_text(self):
        self.assertEqual(c.parse_keys('\x1b[?62;4;22cq'), ['q'])

    def test_titles_cannot_make_imagemagick_read_files(self):
        self.assertEqual(c.caption_text('@/etc/passwd'), '\\@/etc/passwd')
        self.assertEqual(c.caption_text('100% Pure'), '100%% Pure')

    def test_theme_falls_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'colors.toml'
            path.write_text('accent = "#FF8800"\nnonsense = 3\n')
            theme = c.load_theme(path)
        self.assertEqual(theme['accent'], '#ff8800')
        self.assertEqual(theme['background'], c.DEFAULT_THEME['background'])


class Interface(unittest.TestCase):
    """Drives the app the way the keyboard does, with state kept in a temporary folder."""

    ITEMS = [
        {'id': 'film-a', 'title': 'Alpha', 'kind': 'watch', 'tag': 'Space', 'seconds': 1800, 'meta': '30 min'},
        {'id': 'film-b', 'title': 'Beta', 'kind': 'watch', 'tag': 'History', 'seconds': 3600, 'meta': '1 h'},
        {'id': 'mag', 'title': 'Monthly', 'kind': 'read', 'tag': 'Magazine', 'meta': '50 pages'},
        {'id': 'game', 'title': 'Tested Game', 'kind': 'atari', 'verified': True},
        {'id': 'risky', 'title': 'Untested Game', 'kind': 'atari', 'verified': False},
    ]

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        root = Path(self.dir.name)
        self.saved = (c.HISTORY, c.STATE, c.WATCH_LATER)
        c.HISTORY, c.STATE, c.WATCH_LATER = root / 'history.json', root / 'state.json', [root / 'watch_later']
        self.addCleanup(lambda: setattr(c, 'HISTORY', self.saved[0]))
        self.addCleanup(lambda: setattr(c, 'STATE', self.saved[1]))
        self.addCleanup(lambda: setattr(c, 'WATCH_LATER', self.saved[2]))
        self.app = c.App([dict(i) for i in self.ITEMS], art=False)

    def keys(self, *keys):
        for key in keys:
            self.assertTrue(self.app.on_key(key))

    def titles(self):
        return [i['title'] for i in self.app.visible()]

    def test_home_lists_films_then_reading(self):
        self.assertEqual(self.titles(), ['Alpha', 'Beta', 'Monthly'])

    def test_arrows_browse_and_change_section(self):
        self.keys('right', 'down')
        self.assertEqual(c.SECTIONS[self.app.section][0], 'watch')
        self.assertEqual(self.app.selected, 0)
        self.keys('up', 'up')
        self.assertEqual(c.SECTIONS[self.app.section][0], 'plan')

    def test_untested_games_are_hidden_until_asked_for(self):
        self.app.section = [k for k, _ in c.SECTIONS].index('atari')
        self.assertEqual(self.titles(), ['Tested Game'])
        self.keys('u')
        self.assertEqual(self.titles(), ['Tested Game', 'Untested Game'])

    def test_search_from_home_covers_everything_and_q_is_text(self):
        self.keys('/', 'g', 'a', 'm', 'e', 'enter')
        self.assertEqual(self.titles(), ['Tested Game'])
        self.keys('esc')
        self.assertEqual(self.titles(), ['Alpha', 'Beta', 'Monthly'])
        self.keys('/', 'q')
        self.assertEqual(self.app.query, 'q')

    def test_favourite_is_saved_and_shared_file_keeps_other_keys(self):
        c.save_json(c.HISTORY, {'favorites': [], 'recent': ['x'], 'other': 1})
        self.app.recent = ['x']
        self.keys('f')
        saved = c.load_json(c.HISTORY, {})
        self.assertEqual(saved['favorites'], ['film-a'])
        self.assertEqual(saved['other'], 1)

    def test_planner_prompt(self):
        self.keys('p', '0', ':', '4', '0', 'enter')
        self.assertEqual(c.SECTIONS[self.app.section][0], 'plan')
        self.assertEqual(self.titles(), ['Alpha'])
        self.keys('p', 'x', 'enter')
        self.assertIn('not a flight time', self.app.message)

    def test_quit(self):
        self.assertFalse(self.app.on_key('q'))

    def test_every_section_draws_at_several_sizes(self):
        import io

        class Screen(io.StringIO):
            buffer = io.BytesIO()

        for size in [(200, 60, 12, 24), (112, 34, 10, 20), (80, 24, 10, 20), (40, 10, 10, 20)]:
            self.app.geometry = lambda size=size: size
            for section in range(len(c.SECTIONS)):
                self.app.section, self.app.help = section, section % 2 == 0
                screen = Screen()
                self.app.draw(screen)
                self.assertIn('\x1b[?2026l', screen.getvalue())


if __name__ == '__main__':
    unittest.main()
