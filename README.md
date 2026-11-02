# jka-mcp

An MCP server that runs Jedi Knight: Jedi Academy multiplayer clients based on OpenJK without a window, so an AI coding assistant can test them. It was made for VisionJK. Version 0.3.1 tests menus and gameplay. The assistant can start several named clients and a local dedicated server, send console commands, press buttons for a time measured by the game clock, place the player, read a dump of the level, project world points to the screen, take cropped, zoomed and timed screenshots, record and measure the sound, read a dump of the open menus, move the mouse, press keys (also with real X input for windows of the cgame), and rebuild menus, modules, the renderer or the engine and load them into the running game.

Clients start in the headless backend of gamescope. The server talks MCP over stdio. It uses only the Python standard library. Pillow and numpy are optional, see Requirements.

## Requirements

- Linux
- Python 3.8 or newer
- gamescope (only for the `gamescope` launcher)
- libX11 and libXtst, for the `input` tool (loaded with ctypes, no Python package)
- a game install with an OpenJK or VisionJK client, or a folder that `prepare` builds
- Pillow for `crop`, `zoom`, `sequence` and `compare`. Without it screenshots are sent as PNG at full size.
- numpy for the `audio` tool

`console`, `screenshot`, `log` and `launch` work with any OpenJK client that reads console commands from a piped stdin.

`menu_dump`, `mouse`, `click` and `key` need the VisionJK ui commands `v_uiDump`, `v_uiMouse` (with the `px` option) and `v_uiKey`.

`input` needs no ui command, it sends real X events to the gamescope of the client. It works with the `gamescope` launcher only.

`state`, `project`, `hold`, `view`, `sequence`, `screenshot` with `around`, and `wait` with `kind=ingame` or `kind=time` need the VisionJK cgame commands `v_levelDump` and `v_project`. `view` also needs the extended `setviewpos` of the game module (`x y z yaw [pitch [still]]`). Both are in VisionJK master after 2026-11-02.

## Setup

Copy the example config and edit it.

```
cp jka-mcp.example.json jka-mcp.json
```

The server reads the file named by the environment variable `JKA_MCP_CONFIG`. Without it, it reads `jka-mcp.json` next to `jka_mcp.py`. Keys you leave out keep their default. The `configure` tool can change this for one server process, see below. Objects such as `env`, `presets` and `reload` are merged key by key with the default.

| Key | Meaning | Default |
|-----|---------|---------|
| `game_dir` | Folder with the game executable and `base/`. Required. `prepare` can build it. | empty |
| `binary` | The client executable, relative to `game_dir`. | `./visionjk.x86_64` |
| `server_binary` | The dedicated server executable, relative to `game_dir`. | `./visionjkded.x86_64` |
| `home` | `fs_homepath` of the client `main` (configs, screenshots). Other clients use `<home>-<name>`, the server uses `<home>-server`. | `<game_dir>/../jka-mcp-home` |
| `fs_game` | Mod folder the client ends up in, where `screenshots/` is. | `japlus` |
| `launcher` | `gamescope` (headless, no window) or `direct` (a window on the desktop). The server always starts directly. | `gamescope` |
| `env` | Extra environment variables for the game. | `{}` |
| `args` | Command line words added to every client start. | `+set com_bootlogo 0`, `v_updateCheck 0`, `cl_motd 0`, `com_skipIntro 1` |
| `quiet` | Cvars of a launch without `audio=true`. | `s_volume 0`, `s_musicvolume 0` |
| `cvars` | Cvars of every client launch. | `con_notifytime 0` |
| `presets` | Named cvar sets that `launch` can ask for with `preset`. | `msaa8`, `no_spawn_protection` |
| `renderer` | Value for `cl_renderer`, for other OpenJK based clients. VisionJK has no `cl_renderer` anymore, so leave it empty for VisionJK. | empty |
| `server_args` | Command line words added to every server start. | `+set sv_pure 0`, `g_gametype 0` |
| `server_port` | First UDP port the server tries, see `server_launch` below. `connect=localhost` goes to the port the server really got. | `29070` |
| `prepare` | What the `prepare` tool builds, see below. | both empty |
| `reload` | What the `reload` tool runs, see below. | all empty |
| `log_file` | File that also receives the output of client `main`. Other clients and the server write `jka-mcp.log` in their own home. | `<home>/jka-mcp.log` |

The `prepare` object builds a game folder of its own, so nobody has to test in the folder the game is played from.

| Key | Meaning |
|-----|---------|
| `link` | List of `[source dir, destination dir under game_dir, glob]`. Each match becomes a symlink. The glob is optional (default `*`). |
| `copy` | List of `[file, destination relative to game_dir]`. Real copies of the executables, the renderers, the modules and the asset pk3. A copy gets the mode of its source. A copy whose source is its own destination is refused. |

The `reload` object has these keys. Each kind has a list of build commands (argv lists) and a list of files to put in place.

| Key | Meaning |
|-----|---------|
| `cwd` | Working folder for the build commands. |
| `menus` | Commands that build the menus. |
| `menus_copy` | List of `[source dir, destination]`. Every file in a source dir is copied to its destination under `<home>/<fs_game>/` of every client of the session (running or not) and of `main`. The dedicated server gets none, it has no menus. |
| `menus_put` | List of `[built file, destination relative to game_dir]`, e.g. the asset pk3. The game reads it at the next launch. |
| `modules`, `modules_copy` | Build commands and `[built file, destination relative to game_dir]` for the ui, cgame and game libraries. |
| `renderer`, `renderer_copy` | The same for the renderer libraries. |
| `engine`, `engine_copy` | The same for the client and server executables. |

Register the server with Claude Code. The `--transport` option must come before the name.

```
claude mcp add --transport stdio --scope user jka --env JKA_MCP_CONFIG=/path/to/jka-mcp.json -- python3 /path/to/jka_mcp.py
```

## Tools

Most tools take `client` (default `main`) to pick one of several running clients. Pixel coordinates always refer to the full window size, also when a screenshot was scaled down.

| Tool | What it does | Main arguments |
|------|--------------|----------------|
| `launch` | Starts a client. Without `map` or `connect` it waits for the first menu. | `client`, `width`, `height` (default 1920x1080), `map`, `connect`, `renderer`, `preset`, `cvars`, `audio`, `vk_debug`, `extra` |
| `quit` | Ends a client and its process group. | `client`, `all` (every client and the server) |
| `server_launch` | Starts the dedicated server on a map on a free UDP port. | `map`, `gametype` (default 0), `port`, `cvars`, `extra` |
| `server_quit` | Ends the dedicated server. | none |
| `console` | Runs console commands and returns what the game printed. | `command`, `until` (regex of a line to wait for), `timeout`, `wait`, `server` |
| `wait` | Waits for a state, see below. | `kind`, `pattern`, `name`, `value`, `ms`, `join`, `timeout`, `server` |
| `log` | Lines the game printed. Returns a cursor for `since`. | `lines`, `since`, `pattern`, `server` |
| `screenshot` | Picture of the game window. Returns an id for `compare`. | `max_width` (default 1280, 0 is full size), `quality`, `fit`, `crop`, `around`, `size`, `extent`, `zoom` |
| `sequence` | Several screenshots at game times, side by side in one picture. | `at_ms` (or `times`), `hold`, `max_width`, `crop`, `around`, `size`, `extent`, `zoom` |
| `compare` | Two kept screenshots side by side. | `a`, `b` (ids), `crop`, `zoom`, `max_width` |
| `state` | Game time, own player, view and entities near the view (`v_levelDump`). | `radius` (default 1024), `format` (`concise` or `json`) |
| `project` | Window pixels of world points (`v_project`), null when behind the view. | `points` |
| `hold` | Presses buttons for a time of game clock, then lets them go. | `buttons`, `ms` (default 500), `screenshot` |
| `view` | Puts the player at a place and view without the push of a teleport unless `still` is false. Yaw and pitch that are not given stay as they are. Third person camera. | `origin`, `yaw`, `pitch`, `still` (default true), `third`, `range`, `angle`, `screenshot` |
| `audio` | Records the sound and returns loudness and main frequencies per window. | `seconds` (default 2), `window_ms` (default 100), `hold` |
| `menu_dump` | Open menus and their items: name, type, rect, flags, cvar and value, text. | `filter`, `format`, `decorations` |
| `mouse` | Moves the menu pointer to window pixels and can click, hold, release or use the wheel. | `x`, `y`, `action`, `screenshot` |
| `input` | Real mouse and key input through the X display of the client, for cgame windows and the game itself. | `x`, `y`, `home`, `action`, `button`, `count`, `key`, `text`, `key_down`, `key_up`, `step_px`, `screenshot` |
| `click` | Clicks the middle of a menu item found by name or text. | `item`, `menu`, `screenshot` |
| `key` | Presses a key, or types text into the focused field. | `key`, `text`, `screenshot` |
| `reload` | Rebuilds and loads into the running clients. | `what` (`menus`, `modules`, `renderer` or `engine`) |
| `relaunch` | Restarts a client with the same options, back on its map, place and view. | `client`, `keep` (cvar names to carry over) |
| `configure` | Changes the configuration for this server process only. Without arguments it shows it. | `file`, `replace`, `set`, `reset` |
| `prepare` | Builds `game_dir` from `prepare.link` and `prepare.copy`. | none |

`launch` with `map` starts with `+devmap`, so cheats are on. `map` and `connect` together are refused. `connect` takes an address. `localhost` and `local` are mapped to `127.0.0.1` and the port of the server of this session (else `server_port`, or the port given after a colon), because the engine reads `localhost` as the server inside its own process. With `map` or `connect` the call returns after the start, use `wait kind=ingame` for the level. `preset` takes names from the config. The cvars of the config, the presets and `cvars` are written to `jkamcp_launch.cfg` in the home folder and the command line execs it, because the engine drops command line words past 32. A cvar value with a double quote is refused, a cfg line cannot hold one. The log of a client is cut back when it is over 20 MB, the old one is kept as `.old`. With `audio=true` the sound goes to a file through the SDL disk driver, and the quiet volumes are not used. With `vk_debug=true` the Vulkan validation layers run, and their messages are lines with `VULKAN` (`log pattern=VULKAN`).

`server_launch` starts at `server_port` (default 29070) and takes the first UDP port that is free, unless `port` is given. A `port` that is taken is an error. The engine moves silently to one of the next 10 ports when its own is taken, and clients would then join the server of another session. So the port is picked first, and the real one is read back from the server (`net_port`). `connect=localhost` in the same session goes to it. Clients take a UDP port too, so the server may land a few ports higher.

`wait` has four kinds. `ingame` waits until the level is loaded and the player plays. With `join` (default true) it sends `team free` again every 3 seconds and closes open menus. `log` waits for a printed line that matches `pattern`. `cvar` waits until the cvar `name` has `value`. `time` waits until the game clock went on by `ms`. The clock is the one of the cgame, not a frame count.

`audio` reads rate, format and channels from the `SDL_AudioSpec` lines of the engine in the log, and refuses anything but `AUDIO_S16LSB` stereo.

`screenshot` and `sequence` with `extent` (world units) frame a box of that half size around the `around` point. The box is as large as it is on the window and is cut at full resolution. `extent` replaces `size`. `screenshot` returns the frame rectangle. With `fit` a small frame is scaled up to `max_width`.

`input` runs `key_down`, moves the pointer to a window pixel, then runs `action`, then `key`, then `text`, then `key_up`. `action` is `click`, `rightclick`, `hold`, `release` (`button` 1 to 3), `wheelup` or `wheeldown` (`count` times). With `home=true` (default) the pointer first runs into the top left corner, so the position is exact. With `home=false` it moves on from the last position of this tool. `key` takes modifiers, e.g. `ctrl+v`, `shift+tab`, `enter`, `esc`, `f1`, `A`. `text` is typed character by character. `key_down` takes a list of key names that are pressed and stay down across calls, `key_up` lets them go. A Shift drag over several calls: `key_down ["shift"]` with `action` hold, a move with `home=false`, then `key_up ["shift"]` with `action` release. Each reply names the keys still held, and held keys are forgotten when the client stops. The cgame and the ui keep the pointer in 640x480 units and take raw mouse motion, so the pointer moves by `step_px` window pixels per unit (default `min(width/640, height/480)`). Use `input` for what `mouse` and `key` do not reach: cgame windows (VisionJK drawing board, boombox window, model browser) and the game itself. With `launcher` set to `direct` it would move the pointer of the real desktop, so it is refused there. Tested: the pointer ends within 2 px of the target at 1920x1080, clicks on ui menu items, shifted characters, Shift held over separate calls typed "AB" and "cd" came out lower case after `key_up`, Enter, and Shift+Esc for the console.

`hold`, `sequence` and `audio` take buttons as `+forward` or `forward`, and release them at the end, also after an error. `sequence` projects `around` once at the start.

`view` reads the current yaw and pitch from `v_levelDump` for the one that is not given. It sends `setviewpos x y z yaw pitch still` (without `still` when `still` is false) and waits up to 3 seconds until the snapshots of the server show the player there. It needs cheats. `third` sets `cg_thirdPerson`, `range` sets `cg_thirdPersonRange`, `angle` sets `cg_thirdPersonAngle`.

`reload` with `menus` runs the menu build, copies the menu files, puts the pk3 and runs `ui_load`, then opens the menus that were open before again (`main` when none was open and no level is loaded). With `modules` it runs the module build, puts the libraries in place and runs `vid_restart`. `renderer` does the same with the renderer libraries. With `engine` the clients are relaunched like `relaunch` does. If no client runs, only the build and copy steps happen.

`relaunch` keeps the map, the place and the view, and the cvars named in `keep`, which are set again after the level loads.

`reload` and `prepare` read the configuration again, so a changed file counts without reconnecting the server.

Every MCP client session runs its own server process. `configure` changes the configuration of this process only, so a session can point at a worktree build without editing the shared `jka-mcp.json` that other sessions use. `file` reads another JSON file. `replace` takes `{"old path": "new path"}` and changes every path in the configuration at or below the old one. `set` puts keys on top (`null` drops a key). `reset` goes back to the file. Without arguments it shows the configuration. It is refused while a client or the server of this process runs. `reload` and `prepare` keep it. It warns when `game_dir` or `home` are still the shared ones while the build differs, so give them their own `replace` or `set`. `JKA_MCP_CONFIG` still works for each server instance.

`launch` refuses a `home` that a game of another session (or a game started by hand) uses.

`prepare` refuses to run while a client or the server runs, refuses when a game of another session runs from `game_dir`, refuses `/tmp`, and refuses a `game_dir` inside a link source folder. It never writes into the source folders. It writes `jkamcp_prepared.json` in `game_dir`, a list of what it made. On the next run it removes only the files and links from that list that the config no longer names. It never removes anything else.

## Example

A typical gameplay check, as tool calls.

```
prepare {}
launch {"map": "mp/ffa1", "preset": ["no_spawn_protection"]}
wait {"kind": "ingame"}
view {"origin": [0, 0, 100], "yaw": 90, "third": true, "range": 120}
hold {"buttons": ["+forward"], "ms": 800}
sequence {"at_ms": [0, 100, 250, 500], "hold": ["+attack"]}
state {"radius": 512}
quit {"all": true}
```

For a check against the local server, call `server_launch {"map": "mp/ffa1"}` first and use `"connect": "localhost"` in `launch` instead of `map`.

## Notes

- The game folder and the home folder must not be under `/tmp`. The client refuses game code from temporary paths.
- `localhost` in `connect` is mapped to `127.0.0.1` and the server port, see above.
- The engine takes at most 32 commands from the command line, so launch cvars go into a cfg.
- `com_bootlogo 0` is in the default args because the opening logos never end without a window.
- Waits for frames are not reliable, the frame rate changes. `hold`, `sequence` and `wait kind=time` use the game clock.
- The volumes are archived in the config of the home folder. A quiet launch leaves them at 0, so a launch with `audio=true` sets them again.
- `con_notifytime 0` is set by default so console lines do not cover the view.
- Modules, renderers, menu files and copies of `prepare` are put in place by copying to a `.new` file and renaming it, so a running game is not broken.
- gamescope may hang on exit with some drivers. `quit` sends `quit` to the game, then kills the whole process group.
- A client name must be unique. `launch` fails while a client of that name runs.

## License

GPL-2.0, the same as VisionJK and OpenJK.
