# Pane, viewer and the capture loop

The site cannot be framed (`frame-ancestors 'none'`, `X-Frame-Options: DENY`), so
the pane never embeds Stitch Slop. It shows a viewer page served by our own relay,
which streams a Chrome window that the plugin owns.

## Components

```
Xvfb :99 ──▶ Chrome (own profile, --app=site) ──CDP──▶ relay ──HTTP──▶ viewer page ──iframe──▶ pane
                                                        └─ /input ◀── clicks and keys
```

`stitchslop_browser(action="start")` starts all of it and returns `viewerUrl`.

## One capture path only

`Page.startScreencast` and `Page.captureScreenshot` come off different pipelines
and grade colour differently. Alternating between them (screencast for motion,
stills for idle) makes the pane flash between two contrasts — a flicker no CSS can
hide. Use a single `Page.captureScreenshot` loop at ~5 fps, never overlapping a
pending capture. Measured after the fix: four consecutive frames at identical mean
luminance (spread 0.0) and numbers ticking steadily.

A fully painted page emits no screencast damage frames at all, which is why the
first attempt needed idle stills — that is the trap. The capture loop has no such
dependency.

## Network gates, in the order they bite

1. **Mixed content / private network.** A page on `https://…` reaching
   `ws://127.0.0.1` is a local-network request. Chrome shows a permission prompt
   that a windowless session cannot answer, and the socket then hangs unopened —
   nothing in the bridge log. Launch Chrome with
   `--disable-features=LocalNetworkAccessChecks,PrivateNetworkAccessSendPreflights,PrivateNetworkAccessRespectPreflightResults`.
2. **CORS on the relay.** The viewer is served *by* the relay, so its own fetches
   are same-origin and need nothing. A plugin pane fetching `127.0.0.1` directly is
   a different document and is usually sandboxed, which is why the pane embeds the
   `viewerUrl` instead of talking to the relay. Opaque (`null`) and `file://`
   origins are not CORS-granted, and `Access-Control-Allow-Private-Network` is
   not sent.
3. **Remote Hermes.** When the user reaches Hermes from another machine, `127.0.0.1`
   inside a pane is *their* computer. The relay binds **loopback only** by default.
   Set `STITCHSLOP_PANE_BIND=tailscale` (or an explicit Tailscale address in
   `100.64.0.0/10` / `fd7a:115c:a1e0::/48`) when a remote browser must reach the
   host. This is an explicit opt-in with a warning: anyone on that tailnet who
   has the viewer URL can see and drive the Chrome session. Wildcards
   (`0.0.0.0`, `::`) and public or LAN addresses are refused. Any URL handed to
   the preview pane must come from `viewerUrl` (which uses `public_host()` and
   includes `?t=`).
4. **Host header.** The relay rejects any `Host` that is not the bind address
   with the listen port, so a DNS-rebinding page cannot use the fixed ports.

## Pane UX

- The pane must open the `viewerUrl` returned by `stitchslop_browser` (it includes
  `?t=`). Unauthenticated probes of the fixed ports are rejected.
- `/viewer` and `/` are served only when `?t=` matches the pane token (constant-time
  compare). `/frame.jpg` and `/ping` require the token as `?t=` or the
  `x-stitchslop-pane` header. The viewer loads frames as
  `/frame.jpg?t=…` because an `<img>` cannot set headers.
- The viewer polls `/frame.jpg` and swaps a single `<img>` only after the next
  frame decodes — no cross-fade, so two frames can never blend.
- Input is forwarded as `{kind:"mouse"|"text"|"key"|"nav"}` to `/input`, gated by
  a per-run token injected into the viewer HTML at serve time. `nav` is limited
  to `https://stitchslop.com` and its subdomains.
- A build marker (the token's prefix) is returned by `/ping` and the viewer reloads
  itself when it changes, so a relay restart does not need a manual refresh.

## Fancy mode (action overlay)

Every action the relay forwards is also written to a small ring buffer and
drawn over the video, so the preview shows what the agent is doing.

**Agent actions must go through the relay.** Input sent straight to CDP
(`WS.send({"method": "Input.*"})`) never reaches the ring buffer, so the pane stays
silent while the pointer moves — that is the one way this feature silently does
nothing. Drive the UI with `browser.pane_click(x, y)` / `browser.pane_type(text)`,
or `pane_input(event)` for a raw event, which post to the relay's `/input` with the
pane token and are therefore both executed *and* animated.

- **What appears.** A click is a cyan pulsing ring with a dark halo and a bright
  core at the exact page coordinate. Inserted text is a rounded bubble that types
  letter by letter with a caret. A key press is a keycap chip at the bottom centre
  (repeat presses of the same key collapse to `×N`). Wheel and `nav` get a small
  ripple or a "reloading" pill. Nothing is laid out in the document flow: the
  effects sit in an `#fx` layer over `#screen`, so the video never shifts.
- **Where events come from.** `Relay.dispatch()` is the single choke point for
  viewer `/input` posts and for Python callers, and it records there. The viewer
  polls `GET /events?since=<id>` at 8/s and gets a bare JSON list of events with
  monotonic ids and a `ts`. `/events` requires the pane token (`x-stitchslop-pane`),
  because typed text passes through it. The buffer holds 256 events and returns
  every buffered event after `<id>`.
- **Pacing.** Only the animation is queued. Input reaches Chrome the instant it is
  posted; the viewer plays one blip every 320 ms and types at ~36 characters/s
  (28 ms/char). Adjacent text events in one poll are merged into one bubble, key
  repeats become one `×N` chip, and a `n queued` badge appears once more than one
  action is waiting. Nothing is dropped; a long burst is worked through in order.
  Events older than the page load are ignored, so a reload does not replay the
  ring buffer.
- **Toggle.** `stitchslop_browser(action="start", overlay=false)` turns it off;
  the flag is stored as `overlay` in `~/.stitchslop/pane/browser.json` (0600) and
  survives a stop/start or a relay restart. Omitting `overlay` keeps the stored
  value. A running relay is flipped live through `POST /overlay`, and `/ping`
  carries the flag so an open viewer follows. The corner `✦ actions` button flips
  it locally without writing the setting; it re-syncs when the relay flag next
  changes.
- **Scope.** The overlay is a rendering of input that was already forwarded; it
  never adds, delays, or reorders a CDP call. If Chrome is disconnected,
  `dispatch()` fails before recording, so nothing is drawn for input that did not
  land.

## Chrome flags the plugin uses

```
--no-sandbox --disable-dev-shm-usage --disable-gpu
--disable-features=LocalNetworkAccessChecks,PrivateNetworkAccessSendPreflights,
  PrivateNetworkAccessRespectPreflightResults,Translate
--remote-debugging-port=<free> --user-data-dir=<plugin data>/profile
--no-first-run --no-default-browser-check --hide-scrollbars
--window-size=1440,900 --window-position=0,0 --app=https://www.stitchslop.com/
```

`--no-sandbox` is required where user namespaces are unavailable; the window is on
a private Xvfb display, not the user's desktop.

The binary is chosen in this order: the Chromium Hermes ships under
`<HERMES_HOME>/tools/chromium-*/chrome-linux64/chrome` (a build known good on this
host), then `PATH`, then platform installs. A newer system Chrome may re-enable the
Local Network Access gate, so the bundled build is preferred deliberately.

Chrome and helper processes get a **minimal environment** — `DISPLAY`, `HOME`,
`PATH`, `TMPDIR`, `LANG`, `XDG_CACHE_HOME` — never the agent's whole process
environment. Handing a browser every API key in the process is a needless exposure,
and `hpd_check` fails a plugin that does it (`child_env_full`).

## Health checks

- `stitchslop_browser(action="status")` → `{chrome, relay, cdpPort, relayPort, pane:{connected, frames}}`
- `curl http://<host>:8791/ping` → `{ok, connected, frames, build}`
- Empty bridge log + no frames = the browser never dialled (see gates above).
- Frames frozen = the capture loop died with the CDP socket; restart the relay.
