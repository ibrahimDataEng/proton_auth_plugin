"""
Implements Saleor's `external_obtain_access_tokens` hook so a phone-number
user, once OTP-verified via proton_identity, receives a normal Saleor
session token — no password involved.

TRUST DESIGN (read before touching _verify_otp / external_obtain_access_tokens):

proton_identity's `POST /otp/verify` takes the RAW `{phone, code}` pair. It
is single-use (the code is consumed atomically on that exact call),
rate-limited, and attempt-capped — see that service's own README. This
plugin calls that endpoint DIRECTLY, server-to-server, with whatever
`{phone, code}` the client submitted as this hook's `data`. That call IS the
one genuine verification attempt; there is nothing else to trust or forge,
because nothing is trusted except this plugin's own first-time call to
proton_identity using its own configured service token.

An alternative, more complex design was considered and rejected: the client
calls proton_identity's `/otp/verify` itself first, then presents this
plugin with some opaque "verification token" as proof, which the plugin
would re-check independently. That was rejected for two reasons: (1)
proton_identity's current `/otp/verify` response is just
`{verified, userId}` — no signed/opaque token exists to hand back, so that
design would need a new proton_identity endpoint before it could be built
at all; (2) even with one, it adds a second trust surface (something has to
vouch that the "verification token" is genuine and hasn't been replayed)
that the direct design here simply doesn't need. The direct design requires
zero proton_identity changes — confirmed by reading its current source
before writing this file, not assumed.

SSRF NOTE: this plugin calls proton_identity with plain `requests`, not
Saleor's own `saleor.core.http_client.HTTPClient`. That wrapper enforces
Saleor's SSRF/IP filter (HTTP_IP_FILTER_ENABLED), which blocks outbound
calls to private/RFC1918 addresses — exactly the kind of Docker-internal
address proton_identity lives at. Confirmed by reading
saleor/core/http_client.py: the filter is scoped to that specific wrapper
class, not a global patch of the `requests` library, so a plain `requests`
call is unaffected — the same reason proton-payments eventually had to
move to public exposure does NOT apply here, because this plugin never
uses that wrapper.
"""

import logging

import requests

from saleor.account.models import User
from saleor.core.jwt import create_access_token, create_refresh_token
from saleor.graphql.account.mutations.authentication.utils import _get_new_csrf_token
from saleor.graphql.account.types import User as UserType
from saleor.graphql.core.utils import from_global_id_or_error
from saleor.plugins.base_plugin import BasePlugin, ConfigurationTypeField, ExternalAccessTokens

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 5


class PhoneOtpAuthPlugin(BasePlugin):
    PLUGIN_ID = "com.tawasiplus.phone-auth"
    PLUGIN_NAME = "Phone OTP Login"
    PLUGIN_DESCRIPTION = (
        "Issues a Saleor session token for a phone number verified via a "
        "one-time code, by calling the external proton_identity service. "
        "See that service's own README for the OTP send/verify contract."
    )
    # Never default-active for a new auth path — must be turned on explicitly
    # via Dashboard after deploy, same precedent as the COD payment app.
    DEFAULT_ACTIVE = False
    CONFIGURATION_PER_CHANNEL = False

    DEFAULT_CONFIGURATION = [
        {"name": "proton_identity_url", "value": None},
        {"name": "proton_identity_service_token", "value": None},
    ]

    CONFIG_STRUCTURE = {
        "proton_identity_url": {
            "type": ConfigurationTypeField.STRING,
            "help_text": (
                "Base URL of the proton_identity service — no trailing "
                "slash, e.g. http://proton_identity:5001 in Docker, or "
                "http://localhost:5001 for local testing."
            ),
            "label": "proton_identity URL",
        },
        "proton_identity_service_token": {
            "type": ConfigurationTypeField.SECRET,
            "help_text": (
                "Bearer token proton_identity requires on every request — "
                "must match that service's own SERVICE_TOKEN exactly."
            ),
            "label": "proton_identity Service Token",
        },
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        configuration = {item["name"]: item["value"] for item in self.configuration}
        self.proton_identity_url = (configuration.get("proton_identity_url") or "").rstrip("/")
        self.proton_identity_service_token = configuration.get("proton_identity_service_token") or ""

    def _verify_otp(self, phone: str, code: str) -> dict | None:
        """
        Calls proton_identity's POST /otp/verify directly — see this module's
        docstring for why that single call IS the trust boundary.

        Returns the parsed {"verified": True, "userId": str | None} body on a
        clean 200, or None on ANY other outcome — wrong code, expired code,
        too-many-attempts, malformed config, network error, timeout, or an
        unexpected status/body. The caller treats every None identically:
        no session, no detail about which of these it was. Distinguishing
        them to the client would let a caller probe which phone numbers
        exist or are mid-verification — the same enumeration risk
        `externalVerify`'s own Saleor docstring exists to avoid.

        FAIL-CLOSED, deliberately, and unlike the storefront login-gate's
        `GET /phones/status/:userId` check (which fails OPEN on a
        proton_identity outage). Those are different questions with
        different costs of being wrong: phone-status only decides "should I
        show a nag screen," so failing open just risks the nag not
        appearing during an outage — mildly annoying, never unsafe. This
        method decides "should I grant this person a session" — failing
        open here would mean minting a valid login on nothing but "I
        couldn't tell," which is a real account-takeover risk. When in
        doubt, this always means no token.
        """
        if not self.proton_identity_url or not self.proton_identity_service_token:
            logger.error("[phone-auth] proton_identity_url/service_token not configured")
            return None

        try:
            response = requests.post(
                f"{self.proton_identity_url}/otp/verify",
                json={"phone": phone, "code": code},
                headers={"Authorization": f"Bearer {self.proton_identity_service_token}"},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as error:
            logger.error("[phone-auth] proton_identity unreachable: %s", error)
            return None

        if response.status_code != 200:
            # Covers the deliberate 400 (wrong/expired code) and 429
            # (too many attempts / rate limited) responses, as well as any
            # unexpected 401/5xx — all fail closed identically.
            return None

        try:
            body = response.json()
        except ValueError:
            logger.error("[phone-auth] proton_identity returned a non-JSON 200 body")
            return None

        if not body.get("verified"):
            return None

        return body

    def external_obtain_access_tokens(
        self, data: dict, request, previous_value: ExternalAccessTokens
    ) -> ExternalAccessTokens:
        if not self.active:
            return previous_value

        phone = data.get("phone")
        code = data.get("code")
        if not isinstance(phone, str) or not phone or not isinstance(code, str) or not code:
            # Malformed input, not a failed verification — still resolves to
            # the same empty result as every other rejection path below, on
            # purpose (see _verify_otp's docstring on why nothing here is
            # allowed to look different from anything else).
            return previous_value

        result = self._verify_otp(phone, code)
        if not result:
            return previous_value

        user_id = result.get("userId")
        if not user_id:
            # Phone ownership was genuinely verified, but no Saleor account
            # is linked to it yet. There is no session to grant — a future
            # "register via phone" flow is a separate feature, not this
            # hook's job. Still the same empty result, not an error: the
            # caller already knows this outcome is possible (proton_identity
            # documents userId:null as a valid response) and decides what a
            # verified-but-unlinked phone means on its own side.
            return previous_value

        # Broad except is deliberate: this is an external trust boundary
        # (proton_identity's response), and every failure mode here — a
        # malformed ID, a deleted/deactivated user, anything unexpected —
        # must fail closed the same uniform way as every branch above,
        # never propagate as an unhandled exception out of a plugin hook.
        try:
            _, pk = from_global_id_or_error(user_id, only_type=UserType, raise_error=True)
            user = User.objects.get(pk=pk, is_active=True)
        except Exception as error:  # noqa: BLE001 — see comment above
            logger.error(
                "[phone-auth] proton_identity returned userId %s but no matching active Saleor user: %s",
                user_id,
                error,
            )
            return previous_value

        # Plain create_access_token/create_refresh_token — the exact same
        # core.jwt functions the standard password-login mutation
        # (CreateToken) itself calls, with the default "saleor" token owner,
        # not an OIDC-style plugin-tagged one. The resulting token is
        # genuinely indistinguishable from a normal login token everywhere
        # else in this stack (middleware refresh, cookie handling, the
        # checkout sub-app's own auth bridge) — confirmed in the prior
        # research session, and re-verified live this session.
        csrf_token = _get_new_csrf_token()
        return ExternalAccessTokens(
            user=user,
            token=create_access_token(user),
            refresh_token=create_refresh_token(user, additional_payload={"csrfToken": csrf_token}),
            csrf_token=csrf_token,
        )
