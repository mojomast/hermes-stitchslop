---
name: stitchslop
description: Drive the Stitch Slop embroidery app through the plugin bridge, stream its window into a pane, and digitize artwork into machine stitch files (DST/PES). Covers pairing, the command surface, ring lettering and export.
license: MIT
---
# Stitch Slop

The design lives in a browser tab, not on a server. This plugin runs the bridge
that tab dials into (protocol 1 of <https://www.stitchslop.com/agent>), streams
that tab into a viewer page, and exposes four tools. The app owns the command
list; this skill covers how to use it well.

## 1. Connect

1. Ask the user to open **⌁ Agent** in Stitch Slop, switch on **Enable Agent
   Connections**, and paste the line (`Origin:` + `Token: tok_…`).
2. `stitchslop_connect(token=…, origin=…)`. First pairing mints a long-lived
   secret at `~/.stitchslop/pairing.json` (0600); later sessions only need
   `stitchslop_connect()`.
3. If the window should be visible, `stitchslop_browser(action="start")` and open
   the returned `viewerUrl` in the preview pane (`desktop_preview`).

Never paste a token into a shell command line; it belongs in the tool argument. The
plugin writes it to `~/.stitchslop/token` at 0600, starts the bridge with
`--token-file`, and deletes the file as soon as the bridge is up, so the credential
never enters a child process's environment.
What decides whether it connects at all: `references/bridge-protocol.md`.

## 2. Drive

- `stitchslop_call(command, args)` runs one verb; the app answers with an
  envelope. `{"ok": true, "changed": false}` is a no-op, not a success.
- Discover verbs with a deliberate typo: `unknown_command` lists every real one.
  Discover a verb's arguments with a bad one: `bad_arguments` returns `expected`.
- Verbs take ids, not ordinals — **ordinals shift** after any add or delete, so
  hold ids across a multi-step edit.
- Images and exports are written to disk for you: a reply's `dataUrl` becomes
  `savedTo`, and `design.export`'s `files[]` become `savedFiles` beside a shared
  `.col` sidecar.

## 3. Digitize artwork

Full playbook with commands and checks: `references/digitizing.md`. Short form:

1. **Prep the art before tracing.** Square canvas, subject centred, furthest
   pixel ~1 mm inside the ring it lives in, highlight specks filled. The tracer
   inherits every artifact — white slivers in hair become holes, a cut edge
   becomes a gouge. `scripts/prep_art.py` does the mask work.
2. `background.set` (base64 data URL + `xMm/yMm/wMm/hMm`) then
   `background.digitize {colors: 2-12, detail: auto|normal|fine}`.
3. Large masses → tatami, thin forms → satin, letters → satin fills
   (`treatment.set`, explicit ids).
4. Verify numerically: `scene.describe` (objects, stitches, `hoopFit`),
   `problems.list` (target 0), then `view.set {mode:"3d"}` to look at it.
5. `design.export {format:"dst"|"pes", deliver:"data"}` and hand over the files.
   Trim count ≈ separate islands; dozens of trims means a slow sew.

## 4. Lettering on a ring

**The catalog font is single-line.** At any cap that fits an arc its stems are
thinner than one stitch, so it stitches as hairlines with gaps. No tracking or
cap tweak fixes it — stop iterating and switch method:

1. Typeset the sentence with a real bold condensed face at the exact arc
   geometry (`scripts/arc_text.py` renders the PNG).
2. Look at that PNG — orientation and spelling are free to catch there.
3. `background.set` it, `background.digitize` it, drop the background. Solid
   satin letters come back like any traced artwork.

Do not use `design.import` of an SVG for this: the importer traces paths as
strokes, so glyphs arrive hollow and simplified.

Measure the reference for radii and the sentence gap, and match its angular
coverage rather than guessing. `text.add` with `arcRadiusMm` anchors on the start
point, not the circle centre — if you stay with app text, draw an open circular
path and pass `path: {ids:[…]}`.

## 5. Pane and viewer

`stitchslop_browser(action="start")` launches Xvfb + a dedicated Chrome + the
screencast relay and returns `viewerUrl`. Open it in the preview pane: the viewer
is same-origin with the relay, so it survives the sandboxing that blocks a plugin
pane from fetching loopback directly. The viewer draws a paced overlay of every
click, key and typed string the agent performs; `overlay=false` on the same call
turns it off and persists, and the corner `✦ actions` button flips it locally.

Remote Hermes (Tailscale, MagicDNS): `127.0.0.1` in a pane means the *user's*
machine. The relay binds loopback **and** the Tailscale address, and the URL must
be quoted from `viewerUrl` so it carries the right host. The single-capture rule
that keeps the video from flickering, and the network gates that stop the tab
dialling at all: `references/pane.md`.

## 6. Pitfalls

| Symptom | Cause and fix |
|---|---|
| Pane can't reach the relay | Sandboxed document: embed `viewerUrl`; don't fetch loopback from the pane |
| Video flashes between two contrasts | Two capture paths running; the relay must use one (`Page.captureScreenshot` loop only) |
| Tab never dials | The app dials on the switch gesture only — toggle it after the bridge is up. Chrome also gates https→loopback behind Local Network Access; launch with the flags in `references/pane.md` |
| `bad_credential` after a page reload | The panel mints a new token; read the current line and restart the bridge with it |
| `hpd_check` flags `credential_env` | The bridge keeps the protocol's env fallback for shell use; the plugin path uses `--token-file`. Expected |
| Chrome picks a newer system build | Binary order is bundled Chromium → PATH → platform; a newer Chrome may re-arm the Local Network Access gate |
| Tool list looks empty | The app sends `{tools, id: null}`; treat `id: null` as "not a reply" |
| Design exceeds hoop | Usually band + text: check `hoopFit` before export |
| Letters hollow after import | SVG import strokes paths; use render-then-trace |
| Face erased by cleanup | Flood-fill swallows large enclosed whites; fill only small components, no big-kernel close |

## References

| File | Purpose |
|---|---|
| `references/bridge-protocol.md` | Handshake, pairing, control plane, the quirks that cost hours |
| `references/digitizing.md` | Full digitizing playbook: prep, trace, treatments, verification |
| `references/pane.md` | Viewer, capture loop, network gates, remote access |
| `scripts/arc_text.py` | Render arc lettering to a PNG the app can trace |
| `scripts/prep_art.py` | Mask cleanup: fill specks, close hairline gaps, scale to fit |
