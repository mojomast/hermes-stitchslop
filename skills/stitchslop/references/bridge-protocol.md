# The bridge: protocol, and the parts that bit us

Published spec: <https://www.stitchslop.com/agent> (protocol 1). This file records
what the implementation does and the failure modes seen in practice.

## Shape

```
Stitch Slop tab ──WebSocket──▶ bridge (loopback 8787-8790)
                                ├─ HTTP control plane on the same port (/status /tools /wait /call)
                                └─ session-<port>.json (0600) holding the control key
```

The bridge never dials the page and never opens a browser. One tab per bridge.

## Handshake

1. The page's first frame is JSON: `{token}` (bootstrap, one-time) and/or
   `{secret}` (stored pairing). Accept either; if both verify, treat it as a
   secret handshake and mint nothing.
2. On a token handshake, reply
   `{"hello":"stitchslop-connector","protocol":1,"port":N,"pairingSecret":"…"}`,
   store the secret hashed, and mark the token hashed-and-redeemed. A redeemed
   token must be refused for **every** origin.
3. Origin check is exact and case-insensitive, trailing slash ignored. A missing
   Origin, or the literal `null` from a sandboxed frame, is a mismatch → 403.
4. A second tab while the first is reachable gets
   `{"error":"busy","busy":true}` and is closed; it then walks to the next port.
   If the incumbent socket is dead, take over and send it `{"displaced":true}`.

## Quirks that cost real time

- **The tool list arrives as `{"tools":[…],"id":null}`.** A parser that treats any
  `id` key as "this is a reply" silently caches nothing — `tools[]` stays empty
  while commands still work. Treat `id: null` as "not a reply".
- **The token rotates** whenever the agent panel is re-opened or the page reloads.
  The bridge only accepts the token it was started with, so a reload means:
  read the current paste line, restart the bridge with the new value. The stored
  pairing secret is what makes later restarts token-free.
- **The tab dials on the switch gesture.** It does not keep retrying forever after
  a failed scan: when the bridge comes up after the switch was flipped, toggle the
  switch off/on (a real pointer event — a DOM `.click()` on the checkbox toggles
  React state but may not run the connect handler).
- **Chrome gates https→loopback.** The page is https, the bridge is
  `ws://127.0.0.1`. Chrome's Local Network Access prompt cannot be answered in a
  windowless session, and the socket then hangs *unopened* (nothing appears in the
  bridge log). Launch Chrome with
  `--disable-features=LocalNetworkAccessChecks,PrivateNetworkSendPreflights,PrivateNetworkAccessRespectPreflightResults`
  (see `pane.md` for the exact flag the plugin uses).
- **A silent dead port is ambiguous.** Empty bridge log = the browser never tried.
  A logged refusal = it tried and was turned away. Opposite fixes.
- **Idle exit:** no page within 30 minutes exits; standalone bridges exit after
  `--agent-idle-minutes` (default 120) with no control-plane traffic.

## Handing over the token

The bootstrap token is a credential: give the bridge `--token-file <path>` (0600).
Never inject it into the process environment — anything running as the same user
can read `/proc/<pid>/environ`. The **user** pastes the token into
`~/.stitchslop/token` at mode 0600, starts the bridge with `--token-file`, and
deletes the file as soon as the bridge is up (the bridge only reads it once).
The model must never write that file. Process-environment token injection is
unsupported in this plugin, even though some protocol examples mention it for
shell demos.

## Control plane

Same port as the WebSocket, plain HTTP, every route needs
`x-stitchslop-key` from the 0600 session file. Requests carrying an `Origin`
header are refused (pages always send one; shells don't).

| Route | Body | Answers |
|---|---|---|
| `GET /status` | — | `{ok, connected, port, origin, protocol, pid, paired, tools[], message}` |
| `GET /tools` | — | the tab's tool list, or `ok:false` |
| `POST /wait` | `{seconds}` | blocks until a tab attaches; `ok:false, error:"timeout"` |
| `POST /call` | `{command, args}` | the tab's envelope, verbatim |

CLI form for shells and tests: `stitchslop_bridge.py [status|tools|wait N|call '<json>']`,
exit `0` on `ok:true`, `1` refusal, `3` no bridge, `64` usage.

## Envelopes

| Envelope | Meaning |
|---|---|
| `ok: true` | ran; `changed:false` is a no-op |
| `error: "bad_arguments"` | schema failure — `expected` holds the real schema |
| `error: "refused"` | legal call, illegal state; `message` is a sentence for the user |
| `error: "threw"` | app bug |
| `error: "unknown_command"` | no such verb; `message` lists every real one |
| `error: "no_tab"` | nothing attached |

`batch {steps:[{command,args}]}` runs several verbs as one undo step.
