# Cabin TUI

Offline flight entertainment in a terminal, laid out like a console's big-screen
mode: full-screen, large type, artwork for whatever is selected and a shelf of
cards to browse with the arrow keys. Films, magazines, puzzles and Atari ST
games in one place.

It grew out of the `atari-games` launcher and is a second front end to the
[Cabin](../cabin) desktop app: it reads the same catalogue and shares its
favourites and recent list. Python standard library only; nothing in it uses
the network.

## Install and run

```sh
bin/install          # links ~/.local/bin/cabin-tui and adds a launcher entry
cabin-tui --window   # its own fullscreen window with large type (what the launcher entry runs)
cabin-tui            # in the terminal you are already in
```

Needs Python 3.10 or later and, for what you actually open, `mpv`, `evince`,
`hatari` and Simon Tatham's `puzzles`.

Pictures and the large titles need a terminal that draws sixel images (foot
does) and ImageMagick. Without them, or with `--no-art`, the same layout shows
text only. `--window` uses foot; set `CABIN_TUI_FONT_SIZE` (default 15) to
change how large everything is. Colours follow the current Omarchy theme.

## Keys

| Key | Does |
| --- | --- |
| Left / Right | Browse the shelf |
| Up / Down, Tab | Change section |
| PgUp / PgDn, Home / End | Jump along the shelf |
| Enter | Watch, read or play |
| `/` | Search; from Home it searches everything |
| `t` | Cycle the topic filter (Space, Roman history, ...) |
| `f` | Save or remove a favourite |
| `w` | Mark a film watched or unwatched |
| `p` | Plan a line-up for the flight time |
| `u` | Show or hide Atari games that were never boot-tested |
| `d` / `J` | Atari: next boot disk / joystick on the arrow keys |
| `r` | Reload the catalogue and theme |
| `?` / `q` | Help / quit |

## What it does for a flight

- **Battery.** Films open in mpv with hardware decoding, and the header shows
  the charge and the time left on battery.
- **Engine noise.** Films play with levelled audio, and English subtitles are
  picked up when the film has them.
- **Resume.** Quit mpv with `q` and the film keeps its place; the list shows
  the time left and Home puts started films first. A film that ran to the end
  is marked watched.
- **Planner.** `p`, then a flight time such as `3:10`, gives the films that
  fit: started ones first, then favourites, then the rest.
- **Untested games.** Atari games whose copy was never boot-checked are hidden
  until you press `u`, so nothing surprises you in the air.

## Before you leave

```sh
cabin-tui --check        # players installed, files present and complete
cabin-tui --plan 3:10    # print a line-up without opening the interface
cabin-tui --list         # ids, kinds and titles
cabin-tui --dry-run ID   # the exact command an item would run
```

`--check` exits non-zero when a player is missing, a file is missing or empty,
or a download was left unfinished (`*.part`).

## Files

| Path | What |
| --- | --- |
| `~/.local/share/cabin/library.json` | Catalogue, written by Cabin's preparation scripts (`CABIN_LIBRARY` overrides it) |
| `~/.local/state/cabin/history.json` | Favourites and recent items, shared with the desktop app |
| `~/.local/state/cabin-tui/state.json` | Watched films and the untested-games setting |
| `~/.local/state/mpv/watch_later/` | mpv's resume positions, read for progress |
| `~/.cache/cabin-tui/` | Pictures prepared for the terminal; safe to delete |
| `~/.local/state/atari-games/` | Writable copies of the Atari disks; the originals are never changed |

## Tests

```sh
python3 -m unittest
```
