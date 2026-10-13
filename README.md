# LoDeskStream (LAN, local-only)

Prototype that streams the host desktop to a browser over WebRTC, sends system audio when Windows loopback capture is available, and sends mouse/keyboard input back to the host.

## Quick start (Windows)

```powershell
cd "C:\Users\rafasouza\OneDrive - Microsoft\Documents\Microsoft Scout\desktop-stream-poc"
python -m venv C:\venvs\lodeskstream
C:\venvs\lodeskstream\Scripts\Activate.ps1
pip install -r requirements.txt
$env:STREAM_TOKEN = "replace-with-a-strong-token"
python .\server.py
```

Open:

- Same machine: `http://localhost:8765`
- Another device in your LAN: `http://<IP_DO_HOST>:8765`

Paste the same `STREAM_TOKEN` in the page and click **Connect**.

If `STREAM_TOKEN` is not set, LoDeskStream generates a session token and prints it in the console at startup.

## Web app install

1. Open `http://localhost:8765` (or your LAN IP).
2. In Edge/Chrome, use **Install app** from the address bar/menu.
3. The installed web app uses the LoDeskStream icon from `web/icons`.

## Windows portable build

```powershell
pip install pyinstaller
python -m PyInstaller --noconfirm --clean --name LoDeskStream --onedir --console --add-data "web;web" .\server.py
```

Run `dist\LoDeskStream\manager\LoDeskStreamManager.exe` for the tray controller, or run `dist\LoDeskStream\LoDeskStream.exe` directly if you want the raw server console.

## Windows installer

The installer is a per-user setup that extracts the portable build to:

```text
%LOCALAPPDATA%\Programs\LoDeskStream
```

It also creates Desktop and Start Menu shortcuts for the tray controller and registers LoDeskStream in Windows Apps & features for uninstall.

To rebuild it:

```powershell
dotnet publish .\installer-src\LoDeskStreamInstaller.csproj -c Release -r win-x64 --self-contained true -p:PublishSingleFile=true -p:EnableCompressionInSingleFile=true -o .\dist\installer
```

## Tray controller

The tray controller starts LoDeskStream automatically and lets you:

- see whether the server is running;
- start/stop the server;
- open the local or LAN URL;
- copy the current session token;
- set a custom password/token or generate a new one;
- exit the tray app, which also stops the server.

## Legacy mobile behavior

Older iOS/WebKit browsers may connect to WebRTC without rendering video. LoDeskStream automatically falls back to MJPEG if WebRTC does not produce visible frames. Touching the remote screen also focuses a hidden local text field so old iPads can show the on-screen keyboard and forward typed text to the host.

On phones and small tablets, the web UI asks users to rotate to landscape. Installed/PWA mode also declares landscape orientation in the web manifest; browser support varies by iOS version.

To uninstall an installed copy, use Windows **Apps & features** or run:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\Programs\LoDeskStream\uninstall-lodeskstream.ps1"
```

## Notes

- No internet is required.
- Anyone with LAN access + token can control your desktop.
- Keep it in a trusted network and use a strong token.
- You can choose which monitor is streamed from the monitor dropdown.
- On the same PC, leave **Full mouse (move+click)** disabled to avoid continuous cursor hijacking. In this mode it sends click + keyboard and restores the local cursor position after clicks.
- The stream draws the host cursor, and the browser shows a remote cursor overlay. Clicks are mapped to the actual rendered screen area, so fullscreen/letterboxed layouts stay aligned.
- WebRTC is used by default for lower latency and audio. If WebRTC negotiation fails, the browser falls back to the previous MJPEG stream automatically.
- Set `STREAM_AUDIO=0` before starting the server to disable system-audio capture.
- In fullscreen, use the draggable hamburger button to slide down the same LoDeskStream controls without leaving fullscreen.
