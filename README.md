# LoDeskStream (trusted LAN only)

Prototype that streams the host desktop to a browser over WebRTC, sends system audio when Windows loopback capture is available, and sends mouse/keyboard input back to the host.

**Development preview:** project licensing and bundled-codec redistribution
requirements are not resolved. Local builds are not cleared for public binary
distribution. No new release is published by the build scripts.

## Supported and unverified environments

| Component | Current scope |
|---|---|
| Host | Windows 11 x64 is the development environment. Windows 10 22H2 compatibility is not yet validated. |
| Source runtime | CPython 3.12; currently exercised with 3.12.10 and `requirements.txt`. Other Python versions are not certified. |
| Build tools | .NET 10 SDK (currently 10.0.403), Node.js 18+ for regression tests, and `requirements-build.txt`. |
| Installed app | Windows x64, per user. Python and .NET are bundled; development SDKs are not required on the target machine. |
| Browser | Current Edge/Chrome. Other browsers are best effort. |
| Old iPads | Best-effort MJPEG compatibility. The exact iOS/model has not been certified, so no minimum iOS version is promised. WebRTC and fullscreen support vary. |

## Quick start (Windows)

```powershell
git clone https://github.com/souz4rafael/LoDeskStream.git
cd .\LoDeskStream
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:STREAM_TOKEN = "replace-with-a-strong-token"
.\.venv\Scripts\python.exe .\server.py
```

Open:

- Same machine: `http://localhost:8765`
- Another device in your LAN: `http://<IP_DO_HOST>:8765`

Paste the same `STREAM_TOKEN` in the page and click **Connect**.

If `STREAM_TOKEN` is not set, LoDeskStream generates a session token and prints it in the console at startup.

## Network and credential safety

By default the server listens on **all interfaces (`0.0.0.0:8765`) over HTTP**.
Anyone who can reach it and knows the token can view the desktop, receive system
audio and control the host mouse and keyboard. Treat this as full desktop access.

Use only a trusted private network and a strong token. **Do not forward port
8765 to the internet or use this as a secure internet remote-access product.**
WebRTC media encryption does not protect the HTTP page, signaling, credentials
or MJPEG stream. Allow firewall access only for the private network you intend
to use; no public-network firewall exception is required or installed by setup.

For access from this machine only, set `STREAM_HOST=127.0.0.1` before starting.
The MJPEG token is still carried in its URL. New application/access logs redact
token values, but URLs remain sensitive and HTTP traffic is not encrypted.
Redaction does not sanitize old log files. The startup console may intentionally
display an automatically generated token; do not share or publish that console
output. Explicitly configured tokens are not echoed by the launcher.

## Web app install

1. Open `http://localhost:8765` (or your LAN IP).
2. In Edge/Chrome, use **Install app** from the address bar/menu.
3. The installed web app uses the LoDeskStream icon from `web/icons`.

## Building a local Windows candidate

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\build.ps1 -PythonExe .\.venv\Scripts\python.exe
```

The pipeline runs regressions and builds the server, tray, portable ZIP and
installer in fresh staging. Native command failures stop the build immediately.
The exact output directory is printed only after the complete candidate is
ready. Existing `dist` artifacts are not used as build inputs. Tool versions,
source provenance and a payload file/hash manifest accompany the candidate;
this is a repeatable pipeline, not a claim of bit-for-bit identical binaries.

Extract `LoDeskStream-Windows-portable.zip`, then run
`manager\LoDeskStreamManager.exe` for the tray controller or `LoDeskStream.exe`
for the server console. Do not mix files from different candidates.

## Windows installer

The installer is a per-user setup that extracts the portable build to:

```text
%LOCALAPPDATA%\Programs\LoDeskStream
```

It also creates Desktop and Start Menu shortcuts for the tray controller and registers LoDeskStream in Windows Apps & features for uninstall.

Setup extracts and verifies a versioned SHA256 payload manifest before stopping
the installed app or replacing files. Replacement is staged beside the target;
the previous files and shortcut/registry metadata are restored if commit fails.
Existing nonempty folders without a recognized installation are refused.
Only processes whose executable paths belong to the target installation are
stopped. Simultaneous setup operations for the same interactive user are blocked.

For isolated development checks, setup supports `--install-dir <absolute path>`,
`--no-shortcuts`, `--no-registration`, and `--quiet`. These options do not
silently replace the normal installation. `--uninstall --install-dir <path>`
uses that installation's marker and scoped helper. Do not store user documents
inside an installation folder; managed application files are replaced on update.

Use the complete pipeline above to rebuild setup. Direct installer builds
require an explicitly supplied fresh `PayloadZip`; an old ZIP is not silently
selected from `dist`.

## Tray controller

The tray controller starts LoDeskStream automatically and lets you:

- see whether the server is running;
- start/stop the server;
- open the local or LAN URL;
- copy the current session token;
- set a custom password/token or generate a new one;
- exit the tray app, which also stops the server.

The tray manages only the server process it launched and identified. An
independently started server is shown as external: its password cannot be
copied/changed and it is not stopped when the tray exits. A different service on
the same port is reported as a port conflict, not a successful LoDeskStream start.

### Running the tray in development

```powershell
dotnet build .\manager-src\LoDeskStreamManager.csproj -c Debug
.\manager-src\bin\Debug\net10.0-windows\LoDeskStreamManager.exe --dev-root "$PWD" --python "C:\venvs\lodeskstream\Scripts\python.exe"
```

Use absolute paths to the source folder and your existing Python environment.
The development tray starts `server.py` instead of looking for a packaged server.
It reads the optional per-user `LODESKSTREAM_DEV_TOKEN` configuration for its own
launches; otherwise it generates a session token. Credentials are not passed on
the command line. An already-running server remains external and is never
restarted, stopped or assigned a new password by this tray.
The virtual-monitor menu is available in both development and packaged modes.

### Optional virtual monitor (local Windows tray only)

**Virtual Monitor On / Off** are available only in the local tray menu. There
is no web/API switch or browser installation control. They enable/disable an
existing compatible **VirtualDrivers Virtual Display Driver** device; they do
not install a driver, create a device, edit its monitor count or configure
Windows display layout. Off affects **all screens from that driver**, which
may also be used by other applications. The tray asks for confirmation before
requesting Windows administrator approval for a narrowly scoped PowerShell PnP
helper. The tray and streaming server are not elevated. UAC cancellation,
command failure and unconfirmed state changes are not reported as success.
Tray exit does not disable or uninstall this independently installed driver.

On/Off stay disabled when the driver is missing, ambiguous, unsupported, needs
attention or cannot be queried. Detection uses the Display class, root-enumerated
`Root\MttVDD`/`MttVDD` hardware ID, `MikeTheTech` provider and the known
`11.30.4.434` driver version from upstream release **25.7.23**, not a friendly-name
match. Multiple matching devices are refused. This is a conservative identity
check, not verification of the installed package's signature or trust. Driver
On means Windows reports the device enabled, not that every virtual screen is
active in Windows Settings. Status is checked when opening the menu and around
changes, not on the server's 1.5-second polling timer.

**Install Virtual Monitor…** is an explicit **manual, external driver-only**
workflow: it explains the requirements and, only after confirmation, opens the
[upstream 25.7.23 release](https://github.com/VirtualDrivers/Virtual-Display-Driver/releases/tag/25.7.23).
LoDeskStream does not automatically download/install drivers, import
certificates, install VDD Control or add another control app. For the supported
Windows x64 host, `VirtualDisplayDriver-x86.Driver.Only.zip` contains an
`NTamd64` INF despite the asset name; ARM64 has a separate package and is not a
validated LoDeskStream host. Review the publisher, architecture and upstream
root-device installation instructions. Staging an INF alone is not device
creation. If Windows rejects the package's signature, stop rather than disabling
signature enforcement or importing certificates to bypass the warning.
Automatic installation and package trust have **not** been validated.
The normal per-user installer remains unchanged and bundles no display driver.
Upstream's MIT license does not resolve this project's other redistribution
restrictions.

Disconnect browser clients before changing display topology. Reconnect after
the change; Connect refreshes the existing **Choose monitor** picker. Windows
display settings may be needed to extend the desktop. Removing an active
monitor can end its stream or reject stale input indices; there is deliberately
no silent fallback of remote control to another physical screen. Reconnect and
select a remaining monitor. Windows can renumber surviving monitor indices,
so disconnecting before the topology change is important; indices are not
persistent display identities. No-display systems and real driver/UAC mutations
remain unvalidated; automated tests use fake probes and parse helper scripts
without changing OS devices.

## Legacy mobile behavior

Older iOS/WebKit browsers may connect to WebRTC without rendering video. LoDeskStream automatically falls back to MJPEG if WebRTC does not produce visible frames. Use **Show keyboard** in the controls (or the fullscreen hamburger menu) to open the on-screen keyboard and forward typed text to the host. Touching the remote screen does not open the keyboard automatically. Older iOS versions may still leave fullscreen when you explicitly open the keyboard.

The web UI remains usable in portrait without a rotation prompt. Installed/PWA mode still declares a landscape preference, and fullscreen requests landscape where supported; browser support varies by iOS version.

To uninstall an installed copy, use Windows **Apps & features** or run:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\Programs\LoDeskStream\uninstall-lodeskstream.ps1"
```

## Streaming performance

Clients viewing the same monitor share one capture producer, which starts when
frames are first requested. It keeps only the latest frame, so slow clients skip
old frames instead of accumulating a delayed queue. Capture resources close on
their owning thread after the last subscriber leaves and the idle grace period
(1 second, plus any in-flight capture/frame interval) ends.

JPEG is encoded once per frame per monitor, only while MJPEG subscribers exist.
WebRTC consumers can receive RGB frames before JPEG encoding completes. Capture,
resizing, JPEG encoding and WebRTC frame conversion run outside the WebRTC event
loop. WebRTC video encoding and system-audio capture remain per peer.
Native BGRA pixels are decoded directly by Pillow instead of first building a
separate Python RGB buffer.

Monitor geometry is cached for up to 1 second. Listing or selecting monitors
forces a refresh; mouse input uses cached geometry without opening capture
devices per event.

The default keeps native resolution, 12 FPS, JPEG quality 65 and optimized JPEG
compression. An optional server-wide profile reduces work for older devices:

| Setting | Default | `STREAM_PROFILE=legacy` |
|---|---|---|
| Maximum frame size | Native | Fits within 1280x720 |
| FPS | 12 | 10 |
| JPEG quality | 65 | 60 |
| JPEG optimization pass | On | Off |

```powershell
$env:STREAM_PROFILE = "legacy"
python .\server.py
```

The profile affects all clients of that server and requires a restart. It does
not change the Windows display resolution. Resizing preserves aspect ratio,
never upscales and keeps normalized input coordinates aligned with the host.
Individual environment variables override the profile: `STREAM_FPS`,
`STREAM_JPEG_QUALITY`, `STREAM_MAX_WIDTH`, `STREAM_MAX_HEIGHT` (0 means unlimited),
and `STREAM_JPEG_OPTIMIZE` (1 or 0). Disabling the JPEG optimization pass can
reduce encoding time at the cost of larger JPEG frames.
The legacy profile primarily reduces network traffic and client decoding work.
Resizing has a host CPU cost, so it is not guaranteed to reduce host CPU usage
for every scene; it is deliberately opt-in.

Run the repeatable synthetic comparison without capturing or saving your desktop:

```powershell
python .\benchmarks\encode_jpeg.py --iterations 12
```

The comparison reports mean CPU time, median wall time and frame size for the
default, fast-JPEG and legacy settings. Measurements include optional resizing,
but exclude desktop capture and network/decoder costs.

## Notes

### Control sessions

Each connection creates an authenticated control session. The browser sends a
heartbeat every 5 seconds; after 15 seconds without input or a heartbeat, the
server releases that session's held mouse buttons and keys. Cleanup runs every
second. Explicit disconnect also releases held input and invalidates pending
requests from that session.

The browser disconnects when its page is hidden or unloaded. Reconnect after
returning to the page on an iPad. A failed heartbeat also disconnects the client.
Another session cannot send input while a session holds mouse buttons or keys.

API clients must create a session with `POST /api/session`, pass the returned
`sessionId` as `X-Session-ID` on `/api/input`, send
`POST /api/session/heartbeat`, and close with `DELETE /api/session`.
All these requests also require `X-Auth-Token`. Old open browser pages must be
reloaded after updating the server.

Input requests are sent one at a time in a per-session queue. Adjacent pending
mouse movements on the same monitor may be combined; button presses, releases,
keyboard events and text retain their order. A failed or timed-out request
disconnects the session rather than continuing with an uncertain input state.

Each `/api/input` JSON object must include an integer `sequence`, starting at 1
and increasing by 1 for every request in that session. Duplicate, missing or
out-of-order values are rejected without executing the event. Do not retry
failed requests in place; close the session and reconnect.

Fullscreen menu controls are excluded from remote input forwarding, including
mouse, touch, wheel and keyboard interactions.

### Keyboard and connection behavior

Physical keyboards send distinct key-down and key-up events, including modifier
keys for shortcuts such as Ctrl+C, Ctrl+V and Shift+Arrow. Losing focus releases
held keys; disconnecting clears their session ownership. Browser- or OS-reserved
shortcuts may not reach the page. The iPad's on-screen keyboard still uses
**Show keyboard** and text input.

Unicode text injection is not supported yet. Unsupported text is rejected with
an explicit error instead of silently omitting characters.

Connection controls reflect connecting, connected, switching, disconnected and
error states. Operations time out instead of leaving controls permanently busy;
late responses from an ended connection cannot restore it. Audio controls are
disabled for MJPEG and WebRTC sessions without available system audio.

### API validation

Input coordinates must be finite numbers from 0 to 1. Mouse buttons must be 0,
1 or 2; key actions must be `down`, `up` or `tap`. Monitor indices must refer to
an available monitor. Text is limited to 4096 characters and scroll amounts to
integers from -1000 to 1000 per event.

Invalid JSON objects or field values return HTTP 400, invalid/expired sessions
or event sequences return HTTP 409, and requests over 256 KiB return HTTP 413.
SDP offers are limited to 128 KiB; failed negotiation releases its peer and
tracks, and a server-side negotiation timeout returns HTTP 504.

### General

- No internet is required.
- Anyone with LAN access + token can control your desktop.
- Keep it in a trusted network and use a strong token.
- You can choose which monitor is streamed from the monitor dropdown.
- On the same PC, leave **Full mouse** off to avoid continuous cursor hijacking. In this mode it sends click + keyboard and restores the local cursor position after clicks. Turn **Full mouse** on when you need continuous cursor movement or click-and-drag.
- The stream draws the host cursor, and the browser shows a remote cursor overlay. Clicks are mapped to the actual rendered screen area, so fullscreen/letterboxed layouts stay aligned.
- WebRTC is used by default for lower latency and audio. Use **Mode: WebRTC / MJPEG** in the controls or fullscreen menu to select a mode before connecting or switch while connected. MJPEG has no audio. If WebRTC negotiation fails or produces no visible video, the browser falls back to MJPEG and updates the mode button.
- Set `STREAM_AUDIO=0` before starting the server to disable system-audio capture.
- In fullscreen, use the draggable hamburger button to slide down the same LoDeskStream controls without leaving fullscreen.

## Regression tests

With the project's Python dependencies installed and Node.js available:

```powershell
python -m unittest discover -s tests -p "test_*.py"
node --test .\tests\input_queue.test.cjs .\tests\keyboard_input.test.cjs .\tests\connection_state.test.cjs
dotnet run -c Release --project .\tests\tray\LoDeskStream.TrayTests.csproj
dotnet run -c Release --project .\tests\installer\LoDeskStream.InstallerTests.csproj
```

The tests mock desktop input and capture; they do not send real mouse or keyboard
events to the host. Virtual-monitor tests use fake command results and parse
PowerShell scripts without running device mutations or prompting for elevation.
They also execute read-only, deliberately unmatched PnP queries and synthetic
failure scripts to verify empty-device handling and diagnostic propagation.
Expected probe failures include bounded error details in the local tray status;
only the specific PnP enumeration `ObjectNotFound` / `CmdletizationQuery_NotFound`
condition is treated as an empty device list, not permissions or other failures.
An optional read-only check of the actual Windows PnP state is available:

```powershell
dotnet run --project .\tests\tray\LoDeskStream.TrayTests.csproj -- --virtual-monitor-probe
```

After building a candidate, run its install lifecycle in a temporary target
without changing real shortcuts or registration:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tests\installer\integration.ps1 -SetupExe "<candidate-directory>\LoDeskStream-Setup.exe"
```

This covers an isolated target on the development machine, not a clean Windows
VM or a certified upgrade from a previous public release. Those checks remain
required before a public binary release.
