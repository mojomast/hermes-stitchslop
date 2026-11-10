# Changelog

All notable changes to stitchslop are recorded here.

## [0.2.2] - 2026-11-10

Catalog-review follow-up to `9b24e147` (v0.2.1), including [mojomast/hermes-stitchslop#2](https://github.com/mojomast/hermes-stitchslop/pull/2).

### Security

- The pane relay now requires the token on `/viewer`, `/`, `/frame.jpg` and `/ping`. `/ping` no longer returns the token. `null` and `file://` origins are dropped, the `Access-Control-Allow-Private-Network` header is no longer sent, and a `Host` that is not this socket's bind address is rejected.
- `nav` only opens `https://stitchslop.com` and its subdomains on port 443, with no URL credentials and no IP hosts.
- `STITCHSLOP_PANE_BIND` refuses wildcards, LAN and public addresses. A Tailscale address (`100.64.0.0/10` or `fd7a:115c:a1e0::/48`) is an explicit opt-in that logs a warning. Loopback always stays bound so local health checks and pane-only pairing keep working.

### Changed

- The skill and connect-tool docs now say the model must never write the token file; the user pastes it by hand.
- NumPy and Pillow moved to an optional `art` extra. They are not part of the core install Hermes will install.

Chrome still launches with `--no-sandbox`. State still lives under `~/.stitchslop`, which every Hermes profile shares.

## [0.2.1] - 2026-11-09

Catalog pin `9b24e147`. First pairing is pane/hand only; `stitchslop_connect` refuses a model-supplied token.
