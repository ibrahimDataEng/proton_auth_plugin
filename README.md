# proton_auth_plugin

Saleor external-auth plugin: mints a normal session token for a phone
number, once it has been OTP-verified via `proton_identity` — no password
involved. This is the **first Python-package-based plugin** in this
project (every prior Saleor customization was a single-file overlay under
`saleor_platform/custom_patches/`, wholesale-replacing one existing core
module). This one is new code, installed via a setuptools entry point,
and never touches `saleor/settings.py` — see "Why a package, not another
custom_patches overlay" below.

## What this is (and isn't)

- **Is**: a Saleor plugin implementing `external_obtain_access_tokens` —
  the same Saleor hook OpenID Connect login uses — so that a phone number
  verified via `proton_identity`'s OTP flow gets back a token
  indistinguishable from a normal `tokenCreate` (password) login.
- **Isn't**: an OTP service. It has no OTP state of its own — sending and
  checking codes is entirely `proton_identity`'s job (`POST /otp/send`,
  `POST /otp/verify`). This plugin only calls the latter.
- **Isn't**: a registration flow. If the verified phone has no linked
  Saleor account, this plugin returns an empty result (no token, no
  error) — "log in via phone for an unregistered number" is a real,
  anticipated case, but building what happens next is separate,
  not-yet-built work.

## Trust design — read this before changing `plugin.py`

`proton_identity`'s `POST /otp/verify` takes the **raw** `{phone, code}`
pair. It is single-use (the code is consumed atomically on that exact
call), rate-limited, and attempt-capped — see that service's own README.

This plugin calls `/otp/verify` **directly, server-to-server**, with
whatever `{phone, code}` the client submitted as the `input` JSON on
Saleor's `externalObtainAccessTokens` mutation. That call *is* the one
genuine verification attempt. There is no separate "prove you verified"
token to trust or forge, because nothing is trusted except this plugin's
own first-time call to `proton_identity`, authenticated with its own
configured service token.

**An alternative, more complex design was considered and rejected**: have
the client call `proton_identity`'s `/otp/verify` itself first, then
present this plugin with some opaque "verification token" as proof, which
the plugin would re-check independently. Two things ruled that out:

1. `proton_identity`'s current `/otp/verify` response is just
   `{verified, userId}` — there is no signed/opaque token to hand back.
   That design would need a **new endpoint on `proton_identity` first**,
   which is explicitly out of scope for this session.
2. Even with one, it adds a second trust surface (something still has to
   vouch that the "verification token" itself is genuine and hasn't been
   replayed) that the direct design here doesn't need at all.

**Net result: this plugin required zero changes to `proton_identity`.**
Confirmed by reading its current source before writing `plugin.py`, not
assumed — if a future session wants the two-step design instead (e.g. to
decouple "prove phone ownership" from "log in," for some other flow),
that is a deliberate redesign, not a gap this session left open.

## Fail-open vs fail-closed — the other thing not to "fix"

The storefront's login-gate (`GET /phones/status/:userId` in
`proton_identity`, called from `proton_storefront`) **fails open**: if
`proton_identity` is unreachable, it reports `hasPhone: true` so an
outage never locks an existing user out of their own account.

This plugin's `_verify_otp` does the **opposite** — it **fails closed**:
any network error, timeout, non-200 response, or malformed body results
in *no token*, identical to a genuinely wrong code. These are different
questions with different costs of being wrong:

- Phone-status decides "should I show a nag screen." Failing open there
  just risks the nag not appearing during an outage — mildly annoying,
  never unsafe.
- This plugin decides "should I grant this person a session." Failing
  open here would mean minting a valid login on nothing but "I couldn't
  tell whether verification actually happened" — a real account-takeover
  risk. When in doubt, the answer here is always "no."

Do not change `_verify_otp` to fail open without understanding this is a
deliberate, load-bearing difference from the storefront's own pattern,
not an inconsistency to reconcile.

## Why a package, not another `custom_patches/` overlay

Every prior Saleor customization in this project (`throttling.py`,
`product_helpers.py`, `actions.py`) is a single file wholesale-replacing
one existing core module, wired in via a `COPY` in `saleor_platform`'s
Dockerfile. A brand-new plugin doesn't fit that shape: Saleor only
discovers plugins two ways —

1. A hardcoded dotted path in `saleor/settings.py`'s `BUILTIN_PLUGINS`
   list, or
2. A setuptools entry point in the `"saleor.plugins"` group, read via
   `importlib.metadata.entry_points(group="saleor.plugins")`
   (`saleor/settings.py:914-921` — confirmed by reading it directly).

There is **no env-var-based plugin registration**. Option 1 means
overlaying `saleor/settings.py` itself — a much bigger blast radius than
any prior patch (it's the file that wires up the entire plugin system,
not an isolated business-logic module). Option 2 — this package — avoids
touching Saleor's own source at all: `saleor_platform`'s Dockerfile just
needs one extra `pip install ./proton_auth_plugin` layer (see "Docker
wiring" below).

One consequence worth knowing: `settings.py`'s discovery loop also does
`INSTALLED_APPS.append(entry_point.name)` — the entry point's own name
(here, `proton_auth_plugin`, matching the package/directory name) gets
added to Django's `INSTALLED_APPS` as a bare app. That's why
`src/proton_auth_plugin/__init__.py` exists and is intentionally close to
empty: it only needs to be *importable* as a minimal Django app, nothing
about this plugin needs models, migrations, or an `apps.py`.

## Config fields (Dashboard → Configuration → Plugins → Phone OTP Login)

| Field | Type | Meaning |
| --- | --- | --- |
| `proton_identity_url` | String | Base URL of `proton_identity`, no trailing slash — `http://proton_identity:5001` in Docker (shared network), `http://localhost:5001` (plus a port-forward, see `proton_identity`'s own README) for local testing. |
| `proton_identity_service_token` | Secret | Must match `proton_identity`'s own `SERVICE_TOKEN` exactly. |

Both are Dashboard-configurable, not hardcoded, so the same installed
package works against local and production `proton_identity` without a
code change — only the two config values differ.

**`DEFAULT_ACTIVE = False`** — this plugin does **not** activate itself.
Same precedent as the `com.tawasiplus.cod-payment` app: a new
authentication path must be turned on deliberately, never silently live
the moment it's installed. Activate via Dashboard, or the one-time Django
shell command in "Deploy steps" below — either way, see the `api`-restart
gotcha further down if you script it instead of using the Dashboard.

## SSRF note

This plugin calls `proton_identity` with plain `requests`, **not**
Saleor's own `saleor.core.http_client.HTTPClient`. That wrapper enforces
Saleor's SSRF/IP filter (`HTTP_IP_FILTER_ENABLED`), which blocks outbound
calls to private/RFC1918 addresses — exactly what a Docker-internal
address like `proton_identity:5001` is. Confirmed by reading
`saleor/core/http_client.py` directly: the filter is scoped to that one
wrapper class, not a global patch of the `requests` library, so a plain
`requests` call is unaffected. This is the same wall `proton-payments` hit
(and eventually worked around by going public) — it does **not** apply
here, because this plugin never uses that wrapper. Do not "fix" this by
switching to `HTTPClient`; that would reintroduce the exact problem this
design avoids.

## Docker wiring (in `saleor_platform`, not done by this session)

This session only produces the installable package. Wiring it into
`saleor_platform`'s image is a small addition to that repo's Dockerfile —
not made here, since this session's repo scope is `proton_auth_plugin`
only:

```dockerfile
# Phone OTP login plugin — see ../proton_auth_plugin's own README for the
# full design. Not activated by default (DEFAULT_ACTIVE = False); activate
# via Dashboard and set its two config fields after deploy.
COPY --chown=saleor:saleor ../proton_auth_plugin /tmp/proton_auth_plugin
RUN pip install --no-cache-dir /tmp/proton_auth_plugin \
  && rm -rf /tmp/proton_auth_plugin
```

`uv` (Saleor's own build-time package manager) is **not** present in the
final `ghcr.io/saleor/saleor:3.23.1` runtime image `saleor_platform`
extends — confirmed by checking `saleor`'s own Dockerfile (`uv` is
installed only in that image's `build-python` stage and never copied into
the final stage) and by `docker exec`-ing into the actual running `api`
container (`which uv` → nothing, `pip --version` → present). Use `pip`.

## Deploy steps (first package-based plugin — spelled out fully as precedent)

1. In `saleor_platform`: add the `COPY`+`pip install` block from "Docker
   wiring" above to the Dockerfile, then `docker compose build api` (or
   the equivalent full-stack build) and bring the stack back up.
2. Confirm the entry point registered — either
   `docker exec <api> python manage.py shell -c "from django.conf import
   settings; print('com.tawasiplus.phone-auth' in str(settings.PLUGINS))"`,
   or check `Query.plugins` as staff over GraphQL.
3. Activate and configure — **Dashboard** (Configuration → Plugins →
   Phone OTP Login → fill in both fields → Active → Save) is the normal
   path and needs no restart afterward. To script it instead (e.g. for a
   repeatable deploy), the equivalent is:
   ```python
   from saleor.plugins.manager import get_plugins_manager
   manager = get_plugins_manager(allow_replica=True)
   manager.save_plugin_configuration(
       "com.tawasiplus.phone-auth",
       None,  # channel_slug — this plugin is CONFIGURATION_PER_CHANNEL = False
       {
           "active": True,
           "configuration": [
               {"name": "proton_identity_url", "value": "<proton_identity's real URL>"},
               {"name": "proton_identity_service_token", "value": "<its real SERVICE_TOKEN>"},
           ],
       },
   )
   ```
   then **restart the process serving GraphQL** — see the gotcha
   immediately below for why that step isn't optional when scripting this.
4. Smoke-test with a real phone: `POST /otp/send` on `proton_identity`,
   then `externalObtainAccessTokens(pluginId: "com.tawasiplus.phone-auth",
   input: "{\"phone\":\"...\",\"code\":\"...\"}")` over GraphQL — expect a
   real `token`/`refreshToken`/`user`.

## Local verification performed this session

Without modifying `saleor_platform`, the package was validated directly
inside the already-running `api` container (a real deploy installs it
during the Docker **build**, running as root before `USER saleor` — so
root is also the correct way to test the install mechanism itself, not
just a workaround):

```bash
docker cp proton_auth_plugin saleor_platform-api-1:/tmp/proton_auth_plugin
docker exec -u root saleor_platform-api-1 python -m pip install --no-cache-dir -e /tmp/proton_auth_plugin
```

(`pip install --user`, tried first, fails with a permission error on this
particular running container — `/home/saleor` isn't writable by the
`saleor` user it's already running as. That's an artifact of installing
into an already-built, already-`USER saleor`'d container post hoc; the
real Dockerfile flow installs as root during the build stage, before
`USER saleor` is set, so this doesn't affect the real deploy.)

```bash
docker exec saleor_platform-api-1 python -c "
import importlib.metadata
for ep in importlib.metadata.entry_points(group='saleor.plugins'):
    print(ep.name, '->', f'{ep.module}.{ep.attr}')
"
# proton_auth_plugin -> proton_auth_plugin.plugin.PhoneOtpAuthPlugin

docker exec saleor_platform-api-1 python manage.py shell -c "
from django.conf import settings
print([p for p in settings.PLUGINS if 'phone-auth' in p])
print('proton_auth_plugin' in settings.INSTALLED_APPS)
"
# ['proton_auth_plugin.plugin.PhoneOtpAuthPlugin']
# True
```

This proves the entry point resolves and Django imports the plugin class
correctly in a fresh process.

### ⚠️ Config/activation changes need an `api` restart to take effect

Not anticipated going in, found by testing: activating the plugin and
setting its config fields via a one-time `manage.py shell` script (a
**separate, fresh process**) persists to the database correctly and is
immediately visible to *another* fresh process — but the **already-running**
`api` container's own long-lived ASGI worker kept serving the plugin as
inactive over real GraphQL calls until the container was restarted
(`docker compose restart api`). Whatever caches `PluginsManager`/plugin
state per-process doesn't pick up a DB-level config change made from
outside that same process. This almost certainly does **not** apply to
activating a plugin through the Dashboard's own `pluginUpdate` mutation
(that mutation runs *inside* the same request-serving process, so
whatever invalidation it does — if any — is exercised normally); it's
specifically an artifact of configuring a plugin out-of-band, e.g. via
a deploy-time Django shell script. If a future session scripts plugin
activation as part of deployment, restart `api` (or whichever process
serves GraphQL) immediately after.

## Testing performed this session — all against a real, running Saleor + proton_identity, not mocked

- Confirmed the plugin ID appears in Saleor's `PLUGINS` setting and
  `Query.plugins` (staff-permissioned GraphQL) after install + activation.
- Activated the plugin and set its two config fields via a one-time
  Django shell script (`manager.save_plugin_configuration(...)`, the same
  API the Dashboard's `pluginUpdate` mutation itself calls), pointed at
  the real `proton_identity` container already running for this stack —
  then restarted `api` per the gotcha above.
- Created a real Saleor user, linked its phone via `proton_identity`'s
  `POST /phones`, then drove a full, real OTP round trip: `POST
  /otp/send` on `proton_identity` directly, read the code from its dev-SMS
  log, then called Saleor's real `externalObtainAccessTokens(pluginId:
  "com.tawasiplus.phone-auth", input: "{\"phone\":...,\"code\":...}")`
  over GraphQL. A genuine token came back; confirmed it resolves the
  correct account via both `me { id email }` and `tokenVerify`, and that
  its decoded JWT payload (`owner: "saleor"`, `type: "access"`) is
  structurally identical to a normal password-login token.
- Confirmed the unregistered-phone case over the same real mutation:
  correct code, genuinely verified, but no `user_phones` row for that
  number → empty result (no token, no error) — not built as a special
  case, this is `userId: null` flowing through the same "no session"
  path as every rejection below.
- Confirmed every rejection path returns the identical empty, no-detail
  result over the real mutation: a wrong code, and replaying an
  already-consumed correct code. A DB-forced *expired* code was verified
  directly against `_verify_otp`/`proton_identity` in the prior OTP
  session (same 400-status branch `_verify_otp` treats identically to a
  wrong code) rather than re-driven through a live mutation call here —
  not re-tested end-to-end through the plugin specifically this session.
- Confirmed fail-closed under a real outage: stopped the
  `proton_identity` container, called the mutation again — `200 OK` with
  an empty result in ~4s (matching `REQUEST_TIMEOUT_SECONDS`), not a
  crash or a hang. Restarted `proton_identity` afterward.
- All test users, phone links, OTP codes, and the temporary staff account
  created for the `Query.plugins` check were cleaned up at the end of the
  session. The plugin itself was left **active and configured** in this
  local dev environment (a deliberate choice — it demonstrates a working
  end state), though a fresh install still defaults to
  `DEFAULT_ACTIVE = False` per the code.

## Known gaps / follow-ups

- `saleor_platform`'s Dockerfile is not yet updated — this session
  produced the package and verified it live by installing directly into
  the already-running container, but the permanent `COPY`+`pip install`
  wiring above (and the matching `docker compose build`) is a small,
  separate follow-up to that repo. The install performed this session
  lives only in that container's current, uncommitted filesystem state —
  it will not survive a rebuild.
- The checkout sub-app's `useUser()`/cookie-bridge behavior was not
  independently re-run against a plugin-minted token this session — there
  is no storefront route yet that could feed one through that real flow
  (see below). Verified structurally instead: the token's decoded JWT
  payload is identical in shape to a normal login token
  (`create_access_token`/`create_refresh_token`, the same functions
  `tokenCreate` itself calls, default `"saleor"` owner) — the checkout
  sub-app's cookie bridge in `proton_storefront`'s `middleware.ts` copies
  whatever is in the `saleor_token` cookie without caring how it was
  minted, so there's no code path that could behave differently for this
  token vs. a normal one. That's a structural inference, not a re-run of
  Session 38's own checkout-recognition test.
- No storefront-side route calls this mutation yet — that's a
  `proton_storefront` session, not started here.
