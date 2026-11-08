<img src="assets/banner.png" alt="stitchslop for Hermes Agent" width="100%">

<p align="center">
  <a href="https://github.com/mojomast/hermes-stitchslop/actions/workflows/ci.yml"><img alt="ci" src="https://github.com/mojomast/hermes-stitchslop/actions/workflows/ci.yml/badge.svg"></a>
  <img alt="license" src="https://img.shields.io/badge/license-MIT-2b91f8">
  <img alt="hermes" src="assets/badges/hermes.svg">
  <img alt="python" src="https://img.shields.io/badge/python-3.10%2B-2b91f8">
  <img alt="deps" src="assets/badges/deps.svg">
  <img alt="tools" src="assets/badges/tools.svg">
</p>

<h3 align="center">Drive the embroidery app from chat.</h3>
<p align="center">
  Watch the real <a href="https://www.stitchslop.com">Stitch Slop</a> editor in a pane, run its own
  commands, digitize artwork, letter a ring, export DST / PES. Four tools, no dependencies,
  nothing leaves the machine.
</p>

<img src="assets/pane.jpg" alt="The Stitch Slop editor running in the Hermes preview pane" width="100%">

<p align="center"><sub>The pane is the actual editor — not a mock-up. Clicks and keys typed into the
pane go to the app, so you can take over mid-design.</sub></p>

---

## Why this exists

Stitch Slop is a browser embroidery digitizer whose designs live in a tab. A chat agent cannot
reach that tab: the site refuses to be framed (`frame-ancestors 'none'`), and even if it didn't, an
agent can't click.

This plugin solves both halves.

- **The tab dials out.** Stitch Slop publishes a bridge protocol
  ([stitchslop.com/agent](https://www.stitchslop.com/agent)): the page opens a loopback WebSocket
  and then accepts JSON commands. This plugin implements that bridge in the standard library.
- **The pane is real.** A plugin-owned Chrome draws the app on a private X display; the pane shows
  a viewer that streams it over CDP and forwards your clicks and keys back.

The result is a loop — you see the design, the agent edits it, you take over whenever you like.

## Install

```sh
hermes plugins install mojomast/hermes-stitchslop
hermes plugins enable stitchslop
```

Then, in Stitch Slop: open **⌁ Agent** → switch on **Enable Agent Connections** → **Copy**.
Paste that line to Hermes, or put the token in a tool call:

```
stitchslop_connect(token="tok_…", origin="https://www.stitchslop.com")
```

First pairing mints a long-lived secret at `~/.stitchslop/pairing.json` (mode 0600). After that,
`stitchslop_connect()` with no arguments reattaches. The token itself never enters a shell command
or a child process's environment: the plugin writes it to a 0600 file, passes `--token-file`, and
deletes it the moment the bridge is up.

## The four tools

| Tool | What it does |
|---|---|
| `stitchslop_status` | Bridge, tab, Chrome, relay and pane in one call. Start here when something looks wrong. |
| `stitchslop_connect` | Starts the bridge, waits for the tab. Takes `token` + `origin` on first use. |
| `stitchslop_call` | Runs one app verb (`scene.describe`, `background.digitize`, `design.export`, …). Images and exports are written to disk and returned as paths. |
| `stitchslop_browser` | Starts/stops the pane: Xvfb + Chrome + capture relay, returns `viewerUrl`. `overlay=false` turns off the paced click/typing overlay. |

The app owns the command list — over 80 verbs. Discover them by asking for one that doesn't exist:
`stitchslop_call("x.list")` answers with every real name.

## How it works

<img src="assets/diagram.png" alt="Architecture: tab → bridge → tools; Chrome → relay → pane" width="100%">

Two paths, both loopback:

- **Commands** — the tab dials the bridge (`ws://127.0.0.1:8787`), the tools speak HTTP to the same
  port with a key from a 0600 session file. The bridge verifies the exact origin, refuses
  page-originated control requests, and treats the bootstrap token as one-time.
- **Pixels** — Chrome is launched headful on a private `Xvfb` display (headless emits no frames for
  this app), and one `Page.captureScreenshot` loop feeds the viewer. Deliberately one capture
  pipeline: mixing screencast frames with stills makes the video flash between two colour grades.

Why the pane embeds a viewer instead of the site: the renderer blocks a sandboxed document from
reaching loopback, and `frame-ancestors 'none'` blocks the site outright. The viewer is served by
the relay, so its fetches are same-origin. It binds loopback **and** the Tailscale address, because
`127.0.0.1` inside a remote browser means the viewer's machine, not the host's.

## Make something real

<img src="assets/stitch.jpg" alt="A two-colour badge digitized through the plugin" width="58%" align="right">

That badge was made end to end from a chat session: artwork composed, traced to stitches, verified,
exported.

- 51 objects, **5,248 stitches**, 83.1 × 83.1 mm inside a 100 mm hoop
- two thread colours, 49 trims, 6.2 m of thread, `problems.list` → 0
- DST 18 KB and PES 12 KB, committed in [`examples/`](examples/) so you can open them in any
  machine or stitch viewer

The moves that got there are written down in the bundled skill:

| | |
|---|---|
| **Prep before tracing** | The tracer reproduces whatever it's given: a highlight sliver becomes a hole, a crop edge becomes a gouge. `prep_art.py` fills specks, closes hairline gaps and scales the subject to its ring. |
| **Measure the reference** | Ring radii and the sentence gap come from sampling the artwork, not from eyeballing it. |
| **Never typeset an arc with a single-line font** | At any cap that fits the ring, its stems are thinner than one stitch. Render a bold face onto the arc (`arc_text.py`), then trace that. |
| **Big masses → tatami, thin forms → satin** | Letter shapes traced from art come back as satin fills. |
| **Look at it in 3D** | A figure can look solid in stitch view only because the source photo shows through at low opacity. |

<br clear="right">

## What ships with it

The plugin bundles a skill, three references and two scripts — so the agent doesn't rediscover the
same pitfalls every session.

```
skills/stitchslop/
├── SKILL.md                  connect → drive → digitize → letter → pane → pitfalls
├── references/
│   ├── bridge-protocol.md    handshake, pairing, control plane, the quirks
│   ├── digitizing.md         prep, trace, treatments, verification, traps
│   └── pane.md               capture loop, network gates, remote access, action overlay
└── scripts/
    ├── arc_text.py           typeset a sentence onto an arc (cw or ccw)
    └── prep_art.py           mask cleanup: fill holes, close gaps, scale to fit
```

Both scripts run standalone too:

```sh
python3 skills/stitchslop/scripts/arc_text.py \
    --text "REBELLION TO TYRANTS" --out ring.png \
    --band-in 33.8 --band-out 42.3 --span 308 --center 270
```

## What it writes, and what it doesn't

Everything stays local:

| Path | Contents |
|---|---|
| `~/.stitchslop/pairing.json` | the long-lived pairing secret (0600) |
| `~/.stitchslop/pane/` | Chrome profile, logs, last render |
| `~/.stitchslop/exports/` | DST/PES and their `.col` sidecar |

- No outbound network beyond the site itself. No telemetry, no analytics, no fonts or CDNs in the
  viewer — it is one self-contained page.
- The relay and bridge bind loopback, plus the tailnet address (override with
  `STITCHSLOP_PANE_BIND`). Treat the viewer as you would the app: anyone who can reach the port can
  see and drive it.
- Chrome gets a **minimal environment** — six variables, not the agent's whole `os.environ`.

## Tests

```sh
python3 -m unittest discover -s tests
```

29 tests, standard library only, plus Pillow and NumPy for the art-geometry checks. They pin the
things that actually broke: the one-time token, the origin allowlist, the control-plane key, the
`{"tools": […], "id": null}` envelope shape, export `files[]` persistence (and path traversal),
the arc typesetter's geometry, and the overlay's event order, token gate and persisted flag.

`hermes plugins validate` and `plugins doctor --ci` pass. `hpd_check` reports 437 o200k schema
tokens (+38 for the optional `overlay` argument) and one finding — `credential_env` on the bridge's
`STITCHSLOP_TOKEN` fallback. That fallback exists because the published protocol documents it for
shell use; the plugin itself never uses it.

## Troubleshooting

| Symptom | Cause & fix |
|---|---|
| Pane says *no relay / failed to fetch* | A sandboxed document can't reach loopback. Embed the `viewerUrl` in the preview pane; don't fetch `127.0.0.1` from the pane. |
| Video flickers between two contrasts | Two capture paths running. Use one `Page.captureScreenshot` loop only. |
| Tab never dials in | The page dials on the switch gesture: flip **Enable Agent Connections** after the bridge is up. Chrome's Local Network Access gate can also hang an https→loopback socket — see the launch flags in `references/pane.md`. |
| `bad_credential` after a reload | The panel mints a new token each time it opens. Read the current line and reconnect. |
| Tool list looks empty | The app sends `{tools: […], id: null}`; `id: null` is not a reply. |
| Design "exceeds hoop" | Usually band + text. Check `hoopFit` before exporting. |

## Contributing

Issues and PRs welcome. Two ground rules: the bridge and relay stay standard-library only, and a
behaviour change lands with a test that would have caught the bug.

If you're extending the pane, read `skills/stitchslop/references/pane.md` first — the network gates
are subtle and cost more debugging time than the stitching does.

## License

MIT © [mojomast](https://github.com/mojomast). Not affiliated with Stitch Slop or Nous Research.
Stitch Slop is a separate product; this plugin speaks its published bridge protocol.
