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

    def test_sixel_size_is_read_from_the_header(self):
        self.assertEqual(c.sixel_size(b'\x1bP0;0;0q"1;1;640;360#0;2;0;0;0'), (640, 360))

    def test_theme_falls_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'colors.toml'
            path.write_text('accent = "#FF8800"\nnonsense = 3\n')
            theme = c.load_theme(path)
        self.assertEqual(theme['accent'], '#ff8800')
        self.assertEqual(theme['background'], c.DEFAULT_THEME['background'])


if __name__ == '__main__':
    unittest.main()
