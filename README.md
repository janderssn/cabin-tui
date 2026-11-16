# Cabin TUI

Offline flight entertainment in a terminal: films, magazines, puzzles and
Atari ST games from one keyboard-driven list. It grew out of the `atari-games`
launcher and is a second front end to the [Cabin](../cabin) desktop app: it
reads the same catalogue and shares its favourites and recent list.

```
 CABIN  offline flight entertainment                          09:26   battery 100%

  Home   Watch   Read   Puzzles   Atari   Favorites   Recent   Plan
 15 items

    The Stuff of Dreams           1h 27m   SPACE
    The Vietnam War               2h 07m   The Stuff of Dreams
    Rome: Origins of an Empire    58 min   NASA JPL · Voyager
```

Python standard library only. Nothing in it uses the network.

## Install and run

```sh
bin/install     # links ~/.local/bin/cabin-tui and adds a launcher entry
cabin-tui
```

Needs Python 3.10 or later and, for what you actually open, `mpv`, `evince`,
`hatari` and Simon Tatham's `puzzles`.

## Keys

| Key | Does |
| --- | --- |
| Left / Right, Tab | Change section |
| Up / Down, PgUp / PgDn | Move through the list |
| Enter | Watch, read or play |
| `/` | Search; from Home it searches everything |
| `t` | Cycle the topic filter (Space, Roman history, ...) |
| `f` | Save or remove a favourite |
| `w` | Mark a film watched or unwatched |
| `p` | Plan a line-up for the flight time |
| `u` | Show or hide Atari games that were never boot-tested |
| `d` / `J` | Atari: next boot disk / joystick on the arrow keys |
| `r` | Reload the catalogue |
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
| `~/.local/state/atari-games/` | Writable copies of the Atari disks; the originals are never changed |

## Tests

```sh
python3 -m unittest
```
