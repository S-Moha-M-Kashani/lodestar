# Who can reach your board

Lodestar holds a private life — work, love, family, health, money — in a
database on your laptop. This page says exactly who can open it, what stops
everyone else, and how to reach it from another machine without undoing any of
that.

There is one sentence to take away: **Lodestar answers on this computer, and
beyond it only on addresses its own configuration names out loud — and it asks
for a password on every one of them.** Today that is loopback plus one fixed
home-network address, so that a second person in this house can open the board
on her own device. Everything below is how that is enforced and what it costs
you.

That second address is **an interim arrangement with a removal date, not the
design.** The intended end is a private network the owner controls (Tailscale or
WireGuard): a stable name, a real certificate, encryption the home Wi-Fi does
not give, and reachability that does not depend on whose Wi-Fi anyone is on.
When that path works for everybody who needs the board, the home-network
publish and its allowlist entries come out again and the boundary is loopback
plus one private name. Read *[The one address beside
loopback](#the-one-address-beside-loopback-interim)* before you copy this
arrangement.

## The trust boundary

Three things stand between a stranger and the board, and they are independent —
each one holds if the others fail.

| | What it does | Where |
| --- | --- | --- |
| **A named binding, loopback first** | The server listens on `127.0.0.1`, and Compose publishes it there — plus, as one deliberate and commented exception, on one literal home-network address. Never on every interface, and never a range, a subnet or an interface name. | `LODESTAR_BIND`, `docker-compose.yml`, `tests/compose.test.js` |
| **A Host allowlist** | Only `localhost`, `127.0.0.1` and `[::1]` on the port actually bound, plus whole names the deployment configured explicitly — today Compose's internal `lodestar:3000` and the two home spellings. Everything else is refused before the router runs. | `LODESTAR_ALLOWED_HOSTS`, `auth/local-auth.mjs` |
| **A password login** | Every private route needs a session — on loopback and on the home address alike. | `LODESTAR_AUTH_PASSWORD` or `…_HASH` |

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

It is also the reason a second device works at all. A browser on another machine
sends the address or the name it was given, which is not a loopback name, so
both home spellings had to be listed explicitly: whole strings, exactly as
typed, no suffix and no wildcard. Adding a name there is a decision somebody
makes and writes down, which is the opposite of a pattern that turns out to
match more than its author meant.

### Why a password on a machine only you use

Because "only you use it" is a description of yesterday, not a guarantee about
tomorrow. A borrowed laptop, an unlocked screen, a second account, a piece of
software running as you, a screen-share — none of those are exotic, and each
one is somebody reading a private journal. It also means that if any of the
network defences above is ever wrong, the failure is embarrassing rather than
catastrophic.

## Setting the password

Two ways, and `.env` (already git-ignored) is where both of them live. The
server reads that file itself, so setting or changing a password is editing one
line and restarting — nothing to export, and nothing to `source`.

**The simple way.** Put the password in `.env`, in quotes:

```
LODESTAR_AUTH_PASSWORD='the password you will type'
```

Minimum eight characters. It is hashed with scrypt when the server starts, and
the running process keeps only the digest — but the plaintext is sitting in
that file, so anyone who can read the file can log in. On a machine where a
private board is worth protecting from the network rather than from other
people with your user account, that is usually the right trade; the file
already holds your OpenRouter and LangSmith keys, which are worth more.

**The stronger way.** Store only the verifier, so a copy of `.env` is something
to crack rather than a way in:

```sh
npm run auth:setup
```

It asks for a password twice without echoing it, and prints one line:

```
LODESTAR_AUTH_PASSWORD_HASH='scrypt$1$16384$8$1$…$…'
```

Put that in `.env` and leave `LODESTAR_AUTH_PASSWORD` empty. The password
itself is then never stored, never accepted as a command-line argument (an
argument is visible in `ps` and lands in your shell history), and never logged.
Losing it means running `npm run auth:setup` again — there is no reset link,
because there is no email and no second factor to send one to.

**Keep the quotes, whichever you use.** Both the shell and docker compose read
a bare `$` as "insert a variable here". Measured on an 85-character hash:
unquoted, it arrives as `scrypt` when the file is sourced and as 56 characters
under compose, and the server then refuses to boot saying the hash is
unreadable — which reads like a typo and is not one.

**Setting both is a refusal, not a preference.** Whichever one lost would lose
in silence, and what that buys is an afternoon spent wondering why a password
you just changed still logs in with the old one. Delete the line you did not
mean.

**The server refuses to boot without either.** Not a warning, not a default
password, not a "first run" mode: no credential, or an unreadable hash, stops
the process before it opens the board file. There is deliberately no way to
switch authentication off — `LODESTAR_AUTH_MODE` has exactly one legal value,
and a typo is a refusal to start rather than a silent fallback.

**What else `.env` may tell the server: only `LODESTAR_*`.** That file is
shared with the brain, and the board has no business holding
`OPENROUTER_API_KEY` — the browser talks to the model through the board's proxy
precisely so that key stays in one service. A value already in the environment
always wins, so a container's own settings and a one-off
`LODESTAR_DEV_KEY=… npm start` are still the last word.

## What is not the boundary

**Your home Wi-Fi is not an authentication boundary, and Lodestar does not
treat it as one.** There is no trusted-network mode, no subnet allowlist, no
"bind to the LAN when the SSID is mine". A network you trust contains a smart
TV, a guest, a printer with firmware from 2019 and whatever your flatmate
installed; membership of it is not a claim about identity, and a design that
reads it as one is wrong in a way that only shows up once.

The one address published beside loopback below is not an exception to that.
It names **an address, not a network**: nothing in Lodestar asks which Wi-Fi
you are on, and being able to reach the door has never been the same as being
able to open it. The password is still the only thing that lets anybody in.

For the same reason there is **no all-interfaces mode and no bundled reverse
proxy.** Putting Lodestar behind nginx on `0.0.0.0` is not configured against,
but nothing here is written for it: the Host allowlist will refuse the proxy's
hostname until you add it, the cookie is not `Secure` because the service is
plain HTTP, and there is no TLS termination in this project. If you do it
anyway, you own that boundary — this page does not describe it.

## The one address beside loopback (interim)

Since 2026-09-03 Compose publishes the board on one literal home-network
address as well as on `127.0.0.1`, and the Host allowlist names that address and
this machine's Bonjour name. That is what lets a second person in the house open
the board on her own laptop or phone. It is written down in three places that
have to agree — the mapping and its comment in `docker-compose.yml`, the
`LODESTAR_ALLOWED_HOSTS` line beside it, and the exception in the configuration
guard `tests/compose.test.js`, which still fails any mapping that is neither
loopback nor that one address, and fails if anything but the board leaves
loopback at all.

**One literal address, never `0.0.0.0`.** Publishing on every interface would
work on every network, which is exactly the failure this whole page exists to
end: the machine is a laptop that goes to universities and cafés. Docker cannot
bind an address that is not present, so naming the address makes the exception
self-limiting — on any other network `docker compose up` fails loudly instead of
quietly serving a private life to the room. The price of that safety is that the
address has to be held still on the machine (Wi-Fi → Details → TCP/IP → *Using
DHCP with manual address*, or a reservation on the router); if it drifts, the
board stops starting at home too, which is the noisy failure again rather than a
silent one.

Four things this costs, all of them chosen rather than discovered:

- **The login crosses the home network in the clear.** Lodestar is plain HTTP.
  The only encryption between her browser and this machine is the Wi-Fi link
  itself — WPA2 Personal, measured on this machine on 2026-09-08, not the WPA3
  that replaced it. Its weakness here is not the cipher but who holds the key:
  on a pre-shared-key network everyone who knows the Wi-Fi password can derive
  the others' session keys, so the board password, the session cookie and the
  cards themselves are readable to anybody already on that network. This is the
  single strongest reason the arrangement is interim.
- **Anyone on the home Wi-Fi can reach the login page.** The address decides who
  sees the door; the password decides who comes in. Five wrong passwords in a
  row lock login for a minute, and no board data is served before a session
  exists — but the door is visible to the smart TV, the guest, and the printer
  with firmware from 2019.
- **One password means one whole board.** There are no accounts, no roles, no
  per-person and no per-board access: whoever has the password sees every board
  and every card on it, and can edit, finish or delete any of them. Sharing the
  password is sharing the journal, in full, both ways. If partial visibility is
  ever wanted it is a different design and a much larger change, not a setting.
- **The board goes where the laptop goes.** While this machine is away from
  home, nobody else can reach the board at all — not an outage, just what a
  service hosted on somebody's laptop is.

**Where this ends, and how far it has got.** The private network below gives a
stable name, a real certificate and end-to-end encryption, and it terminates at
the same loopback service, so the boundary shrinks back to loopback plus one
name. It is **live since 2026-09-12** and is how the owner's own phone reaches
the board. The mapping above nevertheless stays, because the second person's
device is not on the tailnet and this section is about her: the day it is, the
named mapping, its two allowlist entries and the guard's exception are removed
together, and the plain-HTTP hop described here goes with them.

## Reaching your board from another device

The answer that leaves every defence above exactly where it is: **make the other
device part of this machine's loopback, and log in as usual.** Two ways — and
the first of them is where the arrangement above is heading, not an alternative
to it.

### A private network (Tailscale) — in use since 2026-09-12

Install [Tailscale](https://tailscale.com/) on the laptop and on the phone, and
sign both into the same tailnet. Then, on the laptop:

```sh
tailscale serve --bg --https=8443 http://127.0.0.1:3000
```

Tailscale now answers for `https://<machine>.<tailnet>.ts.net:8443`, from your
own devices only. It terminates TLS on this machine with a real certificate and
dials `127.0.0.1:3000` itself, so **no port is published to any network**,
`LODESTAR_BIND` is untouched, and as far as the board is concerned the request
arrived on its own loopback. Lodestar still asks for the password.

**The step that command does not do, and the board answers 403 without it.** The
`Host` header survives the proxy intact — the tailnet name *and* the port — and
the allowlist above is exact-match by design. So the name has to be added to
`LODESTAR_ALLOWED_HOSTS` in `docker-compose.yml`, and the board restarted:

```yaml
LODESTAR_ALLOWED_HOSTS: lodestar:3000,…,mohas-macbook-pro.tailf01a94.ts.net:8443
```

```sh
docker compose up -d lodestar
```

Until that entry exists every request from the phone is refused before a row is
read, and the refusal reads like the board being down rather than like a rule
being obeyed. `tests/compose.test.js` pins the entry, so an edit aimed at the
allowlist's other names cannot take it along.

**Why port 8443.** Port 443 on this tailnet already belongs to another service on
this machine. Any free port works and the certificate is the same one.

**Two things it is worth beyond reach.** The certificate makes the page a *secure
context*, which is the only condition under which a browser hands a site the
microphone — so voice input works on the phone, and over the plain-HTTP home
route it cannot. And the encryption is the tunnel's own, so unlike WPA2 Personal
it does not rest on a key every device in the building already holds.

**What it does not change.** One password is still one whole board: whoever logs
in sees every board and every card, and may edit or delete any of it. The laptop
must be awake and the phone's Tailscale switch on — that is what a service
hosted on somebody's laptop is, not an outage.

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
on `:3000` and both Chroma stores on `:8003`/`:8004` — and the board *also* on
the one named home address described above. This matters more than it looks:
Docker publishes a port by writing its own NAT rules, so a bare `"3000:3000"`
opens the port on every interface **and routes around your host firewall**.
`tests/compose.test.js` fails on any published mapping that is neither prefixed
`127.0.0.1:` nor that one literal address, and it fails if any mapping outside
the board service leaves loopback: nobody opens a vector store in a browser, and
those two hold the same private life one chunk at a time.

Inside the container the app binds `0.0.0.0` (`LODESTAR_BIND`), because Docker
forwards to the container's own address and a loopback bind there is reachable
by nothing at all. The boundary is the host-side mapping, one level up — today
loopback plus the one named home address, and nothing else.

The composed stack supplies no password of its own, in either form: compose
passes `LODESTAR_AUTH_PASSWORD` and `LODESTAR_AUTH_PASSWORD_HASH` through from
your environment with no fallback value, and the container reads the `.env` in
its mounted tree for whichever of them your environment did not name. With
neither, the board stops at boot rather than starting unprotected.

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

## The developer trace

There is a page that shows what the Assistant was actually asked: for each turn,
the system prompt, your question, every tool the model called, everything those
tools returned, and the answer — in the order the model saw them. It is the most
revealing single view of your board that exists anywhere in this project, which
is why it is off unless you deliberately turn it on, and why it has a lock of
its own.

Two switches, and you need both:

```sh
# in .env — the board stores traces and serves the page
LODESTAR_DEV_KEY=$(node -e "console.log(require('crypto').randomBytes(24).toString('base64url'))")
# and the brain files them
BRAIN_TRACE=board
```

Without `LODESTAR_DEV_KEY` the surface does not exist: no table is created, no
trace is stored, and `/dev/trace` answers `404` rather than telling a stranger
there is a page here they cannot see. With it, the page still sits **behind your
ordinary login** — the key is a second lock, not a way around the first — and
asks for the key separately. Unlocking mints a random token that lives only in
the server's memory, so restarting the board locks the page again, and the key
itself never appears in a URL, in the page, in the cookie or in a log.

The traces are read-only. Nothing on that page can change a card, a message or a
chat, and every response is `Cache-Control: no-store`. Turning both switches back
off stops new traces; the ones already filed stay in `assistant.db` until you
remove them.

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

- No user accounts, roles, registration or password reset. One password, one
  whole board — everybody who has it sees and may change everything.
- No TLS. The service is plain HTTP; on loopback, or inside a tunnel or a
  private network, that costs nothing — the wrapper is what encrypts the traffic
  once it leaves the machine. On the interim home-network publish above there is
  no wrapper, and the Wi-Fi's own WPA2 is the only encryption the login gets —
  which every other holder of the Wi-Fi password can see through.
- No third-party auth dependency. The verifier is Node's own `scrypt`; the
  reasoning, and the libraries weighed against it, are in
  `auth/local-auth.mjs`'s *Alternatives considered* note.
- No analytics, telemetry or tracing by default. `BRAIN_TRACING` is `off`
  everywhere and opting in is an explicit act.

## Reporting something

This is a personal project published for demonstration. If you find a real
issue in it, open an issue describing the class of problem rather than a
working exploit.
