# Camera integration

How SiteScope 360 gets imagery, and how to add a new source — a camera, a robot, a drone or a
fixed installation.

---

## The principle

**The platform must never be hostage to one vendor's SDK.**

So the default path requires no camera integration at all:

```
360 camera  →  record  →  copy files off the camera  →  upload  →  pipeline  →  viewer
```

That works with every 360 camera ever made, needs no vendor account, and is what the demo and the
tests exercise. Every other provider is an optimisation on top of it.

---

## The abstraction

```
CameraProvider                                    (app/cameras/base.py)
 ├── LocalUploadProvider     implemented           browser / mobile multipart upload
 ├── FileImportProvider      implemented           SD card, mounted camera, sync folder
 ├── Generic360Provider      partial               Open Spherical Camera (OSC) over HTTP
 ├── Insta360Provider        interface only        needs the vendor SDK
 └── FutureCameraProvider    template              copy this to add one
```

A provider **declares its capabilities** rather than raising when asked to do something it cannot:

```python
@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    key: str
    display_name: str
    transport: CameraTransport
    implemented: bool
    description: str
    supports_live_transfer: bool = False
    supports_remote_trigger: bool = False
    accepted_extensions: tuple[str, ...] = ('.jpg', '.jpeg', '.png', '.mp4', '.mov')
    setup_hint: str | None = None
    config_schema: dict[str, str] = field(default_factory=dict)
```

`GET /api/cameras/providers` returns these verbatim, and the Settings screen renders them. That is
why the UI can hide what a device cannot do instead of offering a button that fails — and why the
OSC and Insta360 adapters show as **Not implemented** rather than pretending.

### Sources are separate from transports

A capture *source* is who or what produced the imagery; a *transport* is how the bytes arrived.
Keeping them apart is what makes a future robot a new enum value plus an adapter, rather than a
schema migration (§47):

```python
class CaptureSource(StrEnum):
    HUMAN_OPERATOR = 'human_operator'
    ROBOT          = 'robot'
    DRONE          = 'drone'
    FIXED_CAMERA   = 'fixed_camera'
```

`capture_sessions.capture_source` already stores it, and the API already accepts it.

---

## What each provider does today

### `local_upload` — implemented

The MVP path. The upload endpoint *is* this provider. It is registered anyway so a capture session
always names its source and the UI can list it beside hardware adapters without a special case.

No setup. `POST /api/captures/{id}/upload` (multipart, one or more files).

### `folder_import` — implemented

Reads 360 images and video from a folder on the **server**: an inserted SD card, a mounted camera,
or a directory something else syncs into.

```bash
# Register the device (admin)
curl -X POST $API/cameras -H "$AUTH" -H 'Content-Type: application/json' -d '{
  "label": "SD card reader",
  "provider_key": "folder_import",
  "transport": "folder_import",
  "config": { "folder": "/mnt/sdcard/DCIM", "recursive": true }
}'

curl "$API/cameras/$DEVICE/discover" -H "$AUTH"
curl -X POST "$API/captures/$SESSION/import?device_id=$DEVICE" -H "$AUTH"
```

One security detail worth stating: the folder comes from the **device registration** stored by an
administrator, never from a request parameter. A client cannot ask the server to walk an arbitrary
path. Filenames are resolved and checked for containment before opening.

### `osc` — partial

Open Spherical Camera is an open specification, so this adapter needs no vendor SDK.
`health()` speaks the documented `/osc/info` endpoint and will report a reachable camera's
manufacturer, model and firmware.

It is marked `implemented: false` because it has **not been verified against physical hardware** in
this build, and real cameras vary in which optional OSC commands they support. Remote triggering
raises `UnsupportedOperation` with a message saying so rather than failing obscurely.

```json
{ "provider_key": "osc", "config": { "host": "192.168.1.1", "port": 80 } }
```

### `insta360` — interface only

Insta360's transfer protocol requires the vendor's own SDK, under its own licence terms.
Declaring the seam without implementing it keeps the platform honest: nothing pretends to support
hardware it has not been tested against, and the class shows exactly where that support slots in.

Use folder import or upload instead.

---

## Adding a provider

### 1. Write the adapter

`app/cameras/providers.py`. Subclass `BaseCameraProvider`, which supplies sensible defaults so you
implement only what you actually support.

```python
class MyCameraProvider(BaseCameraProvider):
    """One-line statement of what this talks to and how."""

    capabilities = ProviderCapabilities(
        key='my_camera',
        display_name='My 360 Camera (Wi-Fi)',
        transport=CameraTransport.WIFI_OSC,
        implemented=True,              # Only true once verified against real hardware.
        description='Pulls recorded files over the camera\'s Wi-Fi HTTP interface.',
        supports_live_transfer=True,
        supports_remote_trigger=False,
        setup_hint='Join the camera Wi-Fi, then register it with {"host": "192.168.1.1"}.',
        config_schema={'host': 'Camera IP address'},
    )

    def discover(self, config: dict[str, object]) -> Iterator[DiscoveredFile]:
        for entry in self._list_remote(config):
            yield DiscoveredFile(
                filename=entry['name'],
                size_bytes=entry['size'],
                open_stream=lambda e=entry: self._open_remote(config, e),
            )

    def open_file(self, config: dict[str, object], identifier: str) -> BinaryIO:
        ...

    def health(self, config: dict[str, object]) -> dict[str, object]:
        return {'provider': self.capabilities.key, 'implemented': True, 'reachable': True}
```

### 2. Register it

```python
_PROVIDERS: dict[str, BaseCameraProvider] = {
    provider.capabilities.key: provider
    for provider in (
        LocalUploadProvider(),
        FileImportProvider(),
        MyCameraProvider(),          # ← added
        ...
    )
}
```

That is the whole integration. **No migration, no API change, no frontend change** — the Settings
screen and the import endpoint both read the registry.

### 3. Checklist before setting `implemented=True`

- [ ] `discover()` returns real files from real hardware.
- [ ] `open_file()` streams rather than buffering — a 360 video is hundreds of megabytes.
- [ ] `open_file()` refuses any identifier that escapes the configured scope.
- [ ] `health()` distinguishes "unreachable" from "misconfigured" and says which.
- [ ] Unsupported operations raise `UnsupportedOperation` with a message a user can act on.
- [ ] `config_schema` documents every key, and no key holds a secret.
- [ ] `supports_live_transfer` / `supports_remote_trigger` are honest.

The last point matters most. `implemented: true` on an unverified adapter puts a button in front
of a site engineer that will fail in the field.

---

## Why imports reuse the upload path

`POST /api/captures/{id}/import` calls the same `store_upload()` as a browser upload. So however
the bytes arrived, they get:

- an extension allow-list check, **and** a magic-byte sniff (an executable renamed `.jpg` is
  refused on its content);
- a size cap enforced *while streaming*, not from a `Content-Length` header;
- a SHA-256 checksum, so a retried import is de-duplicated rather than duplicated;
- a storage key built from validated segments, which is where path traversal is prevented.

A provider that wrote directly to storage would bypass all of that. None do.

---

## Robots, drones and fixed cameras (§47)

The intended future architecture:

```
Robot / drone / fixed camera
        ↓
   360 camera payload
        ↓
   Capture controller        ← a CameraProvider with supports_remote_trigger
        ↓
   Capture session           ← capture_source = robot | drone | fixed_camera
        ↓
   Vision processing         ← unchanged
        ↓
   Site monitoring platform  ← unchanged
```

Robot **control** is deliberately not implemented. What exists is the seam:

- `CaptureSource` already carries the four values, and the database already stores them.
- `CameraProvider.trigger_capture()` is on the protocol; `BaseCameraProvider` raises
  `UnsupportedOperation` until an adapter implements it.
- `FutureCameraProvider` is a worked template that defaults `capture_source` to `ROBOT`.
- A capture session already records GPS, an operator (nullable — a robot has none), a camera
  device and a source, so an autonomous capture needs no new columns.

For a scheduled autonomous capture the missing pieces are a scheduler and a navigation
integration — neither of which touches the pipeline, the API or the viewer.

---

## Recommendations for capture in the field

Not vendor advice; these follow from measurements in
[VISION_PIPELINE.md](VISION_PIPELINE.md).

| | |
| --- | --- |
| **Resolution** | 4096 × 2048 or better. 1024-wide panoramas are scored `poor` for good reason. |
| **Spacing** | A frame every 1–2 m. At 4 m the geometry no longer has enough overlap to relate frames, and mapping will correctly refuse. |
| **Video interval** | 2 s at a normal walking pace ≈ 1.5–2 m between frames. Configurable per session. |
| **Motion** | Walk at a steady pace and avoid stopping to turn on the spot — pure rotation carries no translation information and is detected and skipped. |
| **Lighting** | Temporary lighting is fine; the quality engine flags frames that are genuinely unreadable rather than rejecting a merely dim one. |
| **Format** | JPG, PNG, MP4 or MOV. Equirectangular 2:1 is detected automatically; an XMP `GPano` block makes detection certain. |
| **Consistency** | Revisit the *same* capture points. Date comparison works by matching `location_id`, so a consistent route is what makes history useful. |
