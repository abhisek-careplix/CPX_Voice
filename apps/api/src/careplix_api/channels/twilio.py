"""Twilio Programmable Messaging: send a text, and check that a webhook really came from Twilio.

Signature (``X-Twilio-Signature``): base64 HMAC-SHA1, keyed with the account's Auth Token, of the
full URL Twilio requested followed by every POST parameter as ``key + value``, sorted by key.
Behind Cloud Run the URL the app sees is not always the one Twilio signed (scheme, host), so
:func:`candidate_urls` lists the public forms and any one of them must match.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Iterable
from dataclasses import dataclass

import httpx

from careplix_api.settings import Settings

# Twilio error codes with a meaning of their own (everything else is a plain failure).
UNSUBSCRIBED = "21610"  # the recipient replied STOP to this sender
NOT_A_MOBILE = {"21614", "21211", "21217"}  # invalid or not SMS-capable


class SmsError(Exception):
    def __init__(self, code: str, http_status: int | None = None):
        super().__init__(code)
        self.code, self.http_status = code, http_status


@dataclass
class Sent:
    sid: str
    status: str


def signature(token: str, url: str, params: Iterable[tuple[str, str]]) -> str:
    data = url + "".join(k + v for k, v in sorted(params))
    digest = hmac.new(token.encode(), data.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def verify(token: str, urls: Iterable[str], params: list[tuple[str, str]], given: str) -> bool:
    if not (token and given):
        return False
    return any(
        hmac.compare_digest(signature(token, url, params).encode(), given.strip().encode())
        for url in urls
    )


def candidate_urls(public_base: str, scheme: str, host: str, path: str, query: str) -> list[str]:
    suffix = path + (f"?{query}" if query else "")
    urls = []
    if public_base:
        urls.append(public_base.rstrip("/") + suffix)
    if host:
        urls.append(f"https://{host}{suffix}")
        urls.append(f"{scheme}://{host}{suffix}")
    return list(dict.fromkeys(urls))


class TwilioSms:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.transport = transport

    def _auth(self) -> tuple[str, str]:
        s = self.settings
        if s.twilio_api_key_sid and s.twilio_api_key_secret.get_secret_value():
            return s.twilio_api_key_sid, s.twilio_api_key_secret.get_secret_value()
        return s.twilio_account_sid, s.twilio_auth_token.get_secret_value()

    async def send(self, to: str, body: str, sender: str, status_callback: str | None) -> Sent:
        s = self.settings
        form = {"To": to, "Body": body}
        if s.twilio_messaging_service_sid:
            form["MessagingServiceSid"] = s.twilio_messaging_service_sid
        else:
            form["From"] = sender
        if status_callback:
            form["StatusCallback"] = status_callback
        url = f"{s.twilio_api_base}/2010-04-01/Accounts/{s.twilio_account_sid}/Messages.json"
        async with httpx.AsyncClient(
            transport=self.transport, timeout=httpx.Timeout(15.0, connect=5.0)
        ) as http:
            try:
                response = await http.post(url, data=form, auth=self._auth())
            except httpx.HTTPError:
                raise SmsError("network") from None
        try:
            data = response.json()
        except ValueError:
            data = {}
        if response.status_code >= 400:
            code = str(data.get("code") or f"http_{response.status_code}")
            raise SmsError(code, response.status_code)
        if not data.get("sid"):
            raise SmsError("no_sid", response.status_code)
        return Sent(sid=str(data["sid"]), status=str(data.get("status") or "queued"))
