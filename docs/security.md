# Who can reach your board

Lodestar holds a private life — work, love, family, health, money — in a
database on your laptop. This page says exactly who can open it, what stops
everyone else, and how to reach it from another machine without undoing any of
that.

There is one sentence to take away: **Lodestar answers on this computer only,
and asks for a password even there.** Everything below is how that is enforced
and what it costs you.

## The trust boundary

Three things stand between a stranger and the board, and they are independent —
each one holds if the others fail.

| | What it does | Where |
| --- | --- | --- |
| **Loopback binding** | The server listens on `127.0.0.1`. A peer on the same Wi-Fi has no address to connect to. | `LODESTAR_BIND`, `server.js` |
| **A Host allowlist** | Only `localhost`, `127.0.0.1` and `[::1]` on the port actually bound are answered. Everything else is refused before the router runs. | `auth/local-auth.mjs` |
| **A password login** | Every private route needs a session, including on loopback. | `LODESTAR_AUTH_PASSWORD_HASH` |

Two more sit behind those, for the browser specifically: the session cookie is
`HttpOnly; SameSite=Strict`, and an authenticated `POST`/`PUT`/`PATCH`/`DELETE`
is refused if it carries an `Origin` or `Referer` naming somewhere else.

### Why a Host allowlist, when the server is already on loopback

Because a web page can make *your* browser connect to `127.0.0.1` for it. The
attacker registers a domain, points it at `127.0.0.1`, and serves you a page
from it; your browser then talks to Lodestar from a page the attacker wrote.
That is DNS rebinding, and the one thing the attacker cannot change is the
`Host` header the browser sends — it still says their domain. So the allowlist
is the defence, and it runs before anything reads or writes a card.

### Why a password on a machine only you use

Because "only you use it" is a description of yesterday, not a guarantee about
tomorrow. A borrowed laptop, an unlocked screen, a second account, a piece of
software running as you, a screen-share — none of those are exotic, and each
one is somebody reading a private journal. It also means that if any of the
network defences above is ever wrong, the failure is embarrassing rather than
catastrophic.

## Setting the password

Once, when you first clone the repo:

```sh
npm run auth:setup
```

It asks for a password twice without echoing it, and prints one line:

```
LODESTAR_AUTH_PASSWORD_HASH=scrypt$1$16384$8$1$…$…
```

Put that line in `.env` (already git-ignored) and start the server. The
password itself is never stored, never accepted as a command-line argument (an
argument is visible in `ps` and lands in your shell history), and never logged.
Losing it means running `npm run auth:setup` again — there is no reset link,
because there is no email and no second factor to send one to.

**The server refuses to boot without it.** Not a warning, not a default
password, not a "first run" mode: a missing or unreadable hash stops the
process before it opens the board file. There is deliberately no way to switch
authentication off — `LODESTAR_AUTH_MODE` has exactly one legal value, and a
typo is a refusal to start rather than a silent fallback.

## What is not the boundary

**Your home Wi-Fi is not an authentication boundary, and Lodestar does not
treat it as one.** There is no trusted-network mode, no subnet allowlist, no
"bind to the LAN when the SSID is mine". A network you trust contains a smart
TV, a guest, a printer with firmware from 2019 and whatever your flatmate
installed; membership of it is not a claim about identity, and a design that
reads it as one is wrong in a way that only shows up once.

For the same reason there is **no supported direct-LAN mode and no bundled
reverse proxy.** Putting Lodestar behind nginx on `0.0.0.0` is not configured
against, but nothing here is written for it: the Host allowlist will refuse the
proxy's hostname until you add it, the cookie is not `Secure` because the
service is plain HTTP on loopback, and there is no TLS termination in this
project. If you do it anyway, you own that boundary — this page does not
describe it.

## Reaching your board from another device

The supported answer is: **make the other device part of this machine's
loopback, and log in as usual.** Two ways, both of which leave every defence
above exactly where it is.

### A private network (Tailscale, WireGuard)

Install [Tailscale](https://tailscale.com/) (or plain WireGuard) on the laptop
and on the phone or second computer. Then forward the loopback port over it —
with Tailscale, one command on the laptop:

```sh
tailscale serve --bg 3000
```

The board is then reachable at the machine's name on your tailnet, from your
devices only, and Tailscale terminates the connection locally and dials
`127.0.0.1:3000` itself. Lodestar still asks for the password.

### An SSH tunnel

Nothing to install if you already have SSH:

```sh
# on the other machine
ssh -N -L 3000:127.0.0.1:3000 you@your-laptop
```

Then open `http://localhost:3000` *on that machine*. The tunnel's far end is
the laptop's own loopback, so as far as Lodestar is concerned the request came
from itself — which is why the `Host` header still says `localhost:3000` and
passes the allowlist. Log in as usual.

Both of these are deliberate acts by you, with a credential of their own, and
neither publishes a port to a network. That is the whole difference between
them and `LODESTAR_BIND=0.0.0.0`.

## Docker

`docker compose up` publishes every port to `127.0.0.1` on the host — the board
on `:3000` and both Chroma stores on `:8003`/`:8004`. This matters more than it
looks: Docker publishes a port by writing its own NAT rules, so a bare
`"3000:3000"` opens the port on every interface **and routes around your host
firewall**. `tests/compose.test.js` fails on any published mapping that lacks
the prefix.

Inside the container the app binds `0.0.0.0` (`LODESTAR_BIND`), because Docker
forwards to the container's own address and a loopback bind there is reachable
by nothing at all. The loopback boundary is the host-side mapping, one level up.

The composed stack will not start without `LODESTAR_AUTH_PASSWORD_HASH` in your
`.env`; there is no fallback value.

## The brain

The Assistant's Python service reads cards and files proposals through the same
API the browser uses, so it needs a credential too. It gets a **service token**
rather than your password:

```sh
node -e "console.log(require('crypto').randomBytes(32).toString('base64url'))"
```

Put it in `.env` as `LODESTAR_SERVICE_TOKEN` for the board and
`BOARD_API_TOKEN` for the brain — the same value. The brain then sends it as a
bearer token on every call. Leaving it empty is safe in the boring direction:
the brain gets `401` and the Assistant says the board is unreachable.

Your password never enters the brain's environment, and revoking the brain's
access is a matter of changing one token rather than changing what you type.

## Sessions

- Stored in the server's memory, keyed by a SHA-256 of the token; the raw token
  exists only in the `Set-Cookie` header and your browser's cookie jar.
- `HttpOnly` (no script can read it), `SameSite=Strict` (no cross-site request
  carries it), `Path=/`.
- 12 hours idle, 7 days absolute. Touching a session extends the first and
  never the second.
- Logging out revokes it, and **restarting the server invalidates every
  session.** That is a feature for a personal app: it means there is no second
  durable credential store to protect, and no way for a session to outlive the
  process that issued it.
- Five wrong passwords in a row lock login for a minute. The lockout is on
  login only — a session you already hold keeps working throughout.

## Deletion

The board's other promise is unrelated to networks but belongs on the same
page: **a card is destroyed only by two deliberate acts** — remove it from the
board, then "Delete permanently" from the Trash. As of 2026-09-01 that is
enforced in SQL (`DELETE … WHERE id = ? AND deleted_at IS NOT NULL`) and not
only in the browser's confirm dialog, so a mistaken or malicious call cannot
erase a live card. The chat record has the same two steps and the same single
purge.

## What is deliberately not here

- No user accounts, roles, registration or password reset. One person, one
  password.
- No TLS. The service is plain HTTP on loopback; a tunnel or VPN provides the
  encryption when the traffic leaves the machine.
- No third-party auth dependency. The verifier is Node's own `scrypt`; the
  reasoning, and the libraries weighed against it, are in
  `auth/local-auth.mjs`'s *Alternatives considered* note.
- No analytics, telemetry or tracing by default. `BRAIN_TRACING` is `off`
  everywhere and opting in is an explicit act.

## Reporting something

This is a personal project published for demonstration. If you find a real
issue in it, open an issue describing the class of problem rather than a
working exploit.
