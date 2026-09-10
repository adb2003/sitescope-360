# SiteScope 360 — mobile capture app

An offline-first Expo / React Native app for the §5 capture workflow: select project → building →
floor → zone, start a session, add the files recorded on a 360 camera, finish, and let them upload
themselves when there is signal.

> ## Verification status
>
> This app is **complete and syntax-checked, but has not been executed.** There is no iOS or
> Android toolchain on the machine it was built on, so `expo start` has never run and no screen
> has been seen on a device.
>
> What *has* been verified: every file parses under TypeScript with zero syntax errors, every
> imported package is declared in `package.json`, and the API calls it makes are the same endpoints
> the backend integration suite exercises (`POST /api/captures` with `client_session_id`,
> `POST /api/captures/{id}/upload`, `POST /api/captures/{id}/process`).
>
> Treat it as a working starting point that needs a device run, not as tested software.
>
> **The web app's capture flow at `/capture` is fully verified** and works on a phone browser
> today — responsive, single-column, large touch targets, with its own IndexedDB-backed offline
> queue. If you need mobile capture right now, use that.

---

## Running it

```bash
cd mobile
npm install
npx expo start          # then press i (iOS simulator), a (Android), or scan the QR code
```

Point it at your API in `app.json`:

```json
{ "expo": { "extra": { "apiBaseUrl": "http://192.168.1.50:8000" } } }
```

| Target | Address |
| --- | --- |
| Android emulator | `http://10.0.2.2:8000` (the default — the emulator's alias for the host) |
| iOS simulator | `http://localhost:8000` |
| Physical device | Your machine's **LAN** address, e.g. `http://192.168.1.50:8000` |

A physical device also needs the backend reachable on that interface:

```bash
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

and the device's origin permitted — add it to `CORS_ORIGINS` in `.env`.

---

## What it does

### Sign in
Email and password, exchanged for the same JWT pair the web app uses. Tokens are stored in
**`expo-secure-store`** — the OS keychain — not AsyncStorage. A site tablet is a shared, easily
lost device, and a bearer token sitting in plaintext application storage is a real risk.

### Capture
The §5 flow, with the status block the brief specifies: session id, floor, zone, camera, elapsed
time, file count and size, connection state, GPS, and storage state. Pause, resume, finish and
cancel.

Files are added from the photo library or the file picker. They are **never re-encoded** —
re-compressing a 360 panorama loses exactly the detail the site team needs.

### Upload queue
"3 file(s) waiting for upload", per session, with each file's status and any error. Upload on
demand, or automatically when connectivity returns. Once every file in a session has uploaded, a
**Start processing** button appears.

---

## How offline-first actually works (§32)

Three decisions carry the whole guarantee that *a capture recorded on site is never lost because
the network was not there*.

### 1. The session id is generated on the device

`newSessionId()` produces the same `CAP-YYYYMMDD-HHMMSS-XXXX` shape the backend uses (§33), and
the backend's create endpoint is **idempotent on it** via `client_session_id`.

That is what makes the flow safe to retry. The operator can start a session, record and finish it
entirely offline; a later sync produces exactly **one** session rather than one per attempt. A
create that times out and is retried three times still yields one session.

### 2. Metadata in SQLite, bytes on the filesystem

| | Where | Why |
| --- | --- | --- |
| Sessions, file queue, attempt counts, errors | `expo-sqlite` | Survives an app restart and OS eviction of the JS context. |
| The capture files themselves | `expo-file-system` document directory | A 360 video is hundreds of megabytes; putting it in a database row would be slow and pointless when it is already a file on disk. |

Picked files are **copied** into the app's own document directory before being queued. A URI from
the image or document picker often points into a cache the OS may clear at any moment, so queuing
the original would risk a pending upload whose file has vanished.

### 3. Serial uploads, and a file is deleted only after confirmation

`sync()` registers unsynced sessions first (a file cannot be uploaded to a session the backend has
never heard of), then uploads files **one at a time**. A site connection that has just come back
will not survive six concurrent multi-hundred-megabyte uploads, and serial progress is progress
the operator can see. A failure stops the run rather than hammering through the queue — if this
upload failed for want of signal, so will the next.

A local file is deleted only once the server has confirmed receipt. If an upload fails halfway,
the file is still there and the next attempt starts again.

`expo-network` polls connectivity every 15 s, and an offline → online transition triggers a drain.

---

## Layout

```
mobile/
├── App.tsx                     sign-in gate, two tabs, connectivity, queue badge
├── index.ts                    Expo entrypoint
├── app.json                    Expo config, permissions, apiBaseUrl
└── src/
    ├── lib/
    │   ├── api.ts              JWT client with refresh-on-401; SecureStore tokens
    │   └── queue.ts            SQLite + filesystem offline queue and sync
    ├── screens/
    │   ├── SignInScreen.tsx
    │   ├── CaptureScreen.tsx   the §5 workflow
    │   └── QueueScreen.tsx     pending uploads, retry, start processing
    ├── components/ui.tsx       buttons, cards, fields — 48dp touch targets
    └── theme.ts                mirrors the web app's dark palette
```

Navigation is hand-rolled rather than using a router library. With two screens, a router would add
a dependency, a native-stack build step and a linking config to solve a problem that is one piece
of state.

The API client is separate from the web one on purpose: different constraints (SecureStore vs
localStorage, React Native's `{uri, name, type}` FormData shape, no CORS), and sharing one client
would mean abstracting over both platforms for very little code.

---

## What it deliberately does not do

| | Why |
| --- | --- |
| Process imagery on the phone | Frame extraction and feature detection are CPU-bound and belong on the server. The app uploads and reports. |
| Talk to a camera over Wi-Fi or Bluetooth | The MVP path is *record → transfer → import*, which works with every 360 camera. Camera adapters are a server-side concern; see [CAMERA_INTEGRATION.md](../docs/CAMERA_INTEGRATION.md). |
| Show the 360 viewer | Viewing, comparison, coverage and reporting are the web app's job. The phone is for capture. |
| Manage projects | Read-only project/floor/zone selection. Structure is created in the web app. |

---

## Before shipping it

- [ ] Run on a real device on a real site — the point of the whole thing.
- [ ] Verify a 2 GiB video uploads without exhausting memory (React Native's FormData should
      stream from the file URI).
- [ ] Verify a mid-upload connection loss leaves the file queued and re-uploadable.
- [ ] Verify a force-quit mid-capture leaves the session and files intact.
- [ ] Add a background upload task (`expo-task-manager`) so the queue drains with the app closed.
- [ ] Add app icons and a splash screen (`app.json` currently references none).
- [ ] Decide on token lifetime for field use — a 60-minute access token means the refresh path runs
      often on a long shift.
