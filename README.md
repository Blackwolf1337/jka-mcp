# jka-mcp

An MCP server that runs Jedi Knight: Jedi Academy multiplayer clients based on OpenJK without a window, so an AI coding assistant can test menus. It was made for VisionJK. The game starts in the headless backend of gamescope with `SDL_AUDIODRIVER=dummy`. The assistant can send console commands, take screenshots, read a dump of the open menus with the rectangles of their items in window pixels, move the mouse, press keys, and rebuild menus or modules and load them into the running game.

The server talks MCP over stdio. It uses only the Python standard library. Pillow is optional: with it, screenshots are scaled down and sent as JPEG, without it they are sent as PNG.

## Requirements

- Linux
- Python 3.8 or newer
- gamescope (only for the `gamescope` launcher)
- a game install with an OpenJK or VisionJK client

`menu_dump`, `mouse`, `click` and `key` need the VisionJK ui commands `v_uiDump`, `v_uiMouse` (with the `px` option) and `v_uiKey`. `console`, `screenshot`, `log` and `launch` work with any OpenJK client that reads console commands from a piped stdin.

## Setup

Copy the example config and edit it.

```
cp jka-mcp.example.json jka-mcp.json
```

The server reads the file named by the environment variable `JKA_MCP_CONFIG`. Without it, it reads `jka-mcp.json` next to `jka_mcp.py`. Keys you leave out keep their default.

| Key | Meaning | Default |
|-----|---------|---------|
| `game_dir` | Folder with the game executable and `base/`. Required. | empty |
| `binary` | The executable, relative to `game_dir`. | `./visionjk.x86_64` |
| `home` | `fs_homepath` for the runs (configs, screenshots). | `<game_dir>/../jka-mcp-home` |
| `fs_game` | Mod folder the client ends up in, where `screenshots/` is. | `japlus` |
| `launcher` | `gamescope` (headless, no window) or `direct` (a window on the desktop). | `gamescope` |
| `env` | Extra environment variables for the game. | `{}` |
| `args` | Command line words added to every start. | `com_bootlogo 0`, `v_updateCheck 0`, `cl_motd 0`, `s_volume 0`, `s_musicvolume 0`, `com_skipIntro 1` (all as `+set`) |
| `renderer` | Value for `cl_renderer`. Empty leaves the choice to the game. | `rd-vulkan-vision` |
| `reload` | What the `reload` tool runs, see below. | all empty |
| `log_file` | File that also receives the game output. | `<home>/jka-mcp.log` |

The `reload` object has these keys.

| Key | Meaning |
|-----|---------|
| `cwd` | Working folder for the build commands. |
| `menus` | List of commands (each an argv list) that build the menus. |
| `menus_copy` | `[source dir, destination]`. Every file in the source dir is copied to the destination under `<home>/<fs_game>/`. |
| `modules` | List of commands (each an argv list) that build the modules. |
| `modules_copy` | List of `[built file, destination relative to game_dir]`. |

Register the server with Claude Code. The `--transport` option must come before the name.

```
claude mcp add --transport stdio --scope user jka --env JKA_MCP_CONFIG=/path/to/jka-mcp.json -- python3 /path/to/jka_mcp.py
```

## Tools

| Tool | What it does | Main arguments |
|------|--------------|----------------|
| `launch` | Starts the game and waits for the first menu. | `width`, `height` (default 1920x1080), `extra` (more command line words) |
| `quit` | Ends the game and its process group. | none |
| `console` | Runs console commands and returns what the game printed. | `command`, `until` (regex of a line to wait for), `timeout`, `wait` |
| `screenshot` | Picture of the game window. | `max_width` (default 1280, 0 is full size), `quality` |
| `menu_dump` | Open menus and their items: name, type, rect, flags, cvar and value, text. | `filter`, `format` (`concise` or `json`), `decorations` |
| `mouse` | Moves the menu pointer to window pixels and can click, hold, release or use the wheel. | `x`, `y`, `action`, `screenshot` |
| `click` | Clicks the middle of a menu item found by name or text. | `item`, `menu`, `screenshot` |
| `key` | Presses a key, or types text into the focused field. | `key`, `text`, `screenshot` |
| `reload` | Rebuilds menus or modules and loads them into the running game. | `what` (`menus` or `modules`) |
| `log` | Lines the game printed. Returns a cursor for `since`. | `lines`, `since` |

`reload` with `menus` runs the menu build, copies the menu files and runs `ui_load`. With `modules` it runs the module build, puts the libraries in place and runs `vid_restart`. If the game is not running, only the build and copy steps happen.

Pixel coordinates always refer to the full window size, also when a screenshot was scaled down.

## Notes

- The game folder and the home folder must not be under `/tmp`. The client refuses game code from temporary paths.
- `com_bootlogo 0` is in the default args because the opening logos never end without a window.
- Modules and menu files are put in place by copying to a `.new` file and renaming it, so a running game is not broken.
- Only one game runs at a time. `launch` fails while a game is running.

## License

GPL-2.0, the same as VisionJK and OpenJK.
