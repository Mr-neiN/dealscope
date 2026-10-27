"""Telegram delivery and alert formatting.

The notifier makes exactly one attempt per call; retries are scheduled by the
alert service so a Telegram outage can never permanently suppress a pending
deal. Delivery is only recorded after Telegram confirms the message.
"""

from __future__ import annotations

import html
import time
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

import httpx

from src.money import format_eur


class TelegramError(RuntimeError):
    """Base class for Telegram delivery failures."""


class TelegramPermanentError(TelegramError):
    """The message can never be delivered (bad token, chat, or payload)."""


class TelegramRateLimited(TelegramError):
    def __init__(self, retry_after: float | None, message: str) -> None:
        self.retry_after = retry_after
        super().__init__(message)


class TelegramNotifier:
    def __init__(
        self,
        token: str,
        chat_id: str,
        *,
        http: httpx.Client | None = None,
        base_url: str = "https://api.telegram.org",
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._token = token
        self._chat_id = chat_id
        self._http = http or httpx.Client(timeout=15.0)
        self._base_url = base_url.rstrip("/")
        self._sleep = sleep

    @property
    def configured(self) -> bool:
        return bool(self._token and self._chat_id)

    def close(self) -> None:
        self._http.close()

    def send(self, text: str) -> str | None:
        if not self.configured:
            raise TelegramPermanentError("Telegram token or chat id is not configured")
        url = f"{self._base_url}/bot{self._token}/sendMessage"
        try:
            response = self._http.post(
                url,
                json={
                    "chat_id": self._chat_id,
                    "text": text[:3900],
                    "parse_mode": "HTML",
                    "disable_web_page_preview": False,
                },
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise TelegramError(f"Telegram network error: {exc}") from exc
        if response.status_code == 429:
            retry_after = None
            try:
                payload = response.json()
                retry_after = float(payload.get("parameters", {}).get("retry_after"))
            except (ValueError, TypeError, AttributeError):
                retry_after = None
            raise TelegramRateLimited(retry_after, "Telegram rate limited the bot")
        if response.status_code == 200:
            try:
                payload = response.json()
            except ValueError as exc:
                raise TelegramError("Telegram returned malformed JSON") from exc
            if not isinstance(payload, Mapping) or not payload.get("ok"):
                raise TelegramError("Telegram did not confirm delivery")
            result = payload.get("result")
            if isinstance(result, Mapping) and result.get("message_id") is not None:
                return str(result["message_id"])
            return None
        detail = _telegram_error(response)
        if response.status_code in (400, 401, 403):
            raise TelegramPermanentError(
                f"Telegram rejected the message (HTTP {response.status_code}): {detail}"
            )
        raise TelegramError(f"Telegram HTTP {response.status_code}: {detail}")

    def send_test(self) -> str | None:
        return self.send(
            "<b>eBay scanner test</b>\n\n"
            "Telegram delivery works. Alerts are formatted like this."
        )


def _telegram_error(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:300]
    if isinstance(payload, Mapping):
        return str(payload.get("description") or payload)[:300]
    return str(payload)[:300]


def format_alert(payload: Mapping[str, Any]) -> str:
    title = html.escape(str(payload.get("title") or "Listing"))
    lines: list[str] = [f"<b>Potential deal: {title}</b>", ""]

    is_auction = bool(payload.get("is_auction"))
    if is_auction:
        lines.append(
            f"<b>Current bid:</b> {format_eur(_int(payload.get('price_eur_minor')))}"
            f" ({_int(payload.get('bid_count')) or 0} bids)"
        )
    else:
        lines.append(f"<b>Listing price:</b> {format_eur(_int(payload.get('price_eur_minor')))}")

    shipping_text = format_eur(_int(payload.get("shipping_eur_minor")))
    if not payload.get("shipping_known", True):
        shipping_text += " (estimated)"
    lines.append(f"<b>Shipping:</b> {shipping_text}")
    lines.append(f"<b>Estimated total:</b> {format_eur(_int(payload.get('total_cost_eur_minor')))}")
    lines.append(f"<b>Estimated fair value:</b> {format_eur(_int(payload.get('value_eur_minor')))}")
    lines.append(
        "<b>Estimated discount:</b> "
        f"{_float(payload.get('discount_pct')):.1f}% "
        f"(saves {format_eur(_int(payload.get('savings_eur_minor')))})"
    )
    if is_auction and payload.get("max_bid_eur_minor") is not None:
        lines.append(
            "<b>Maximum rational bid:</b> "
            f"{format_eur(_int(payload.get('max_bid_eur_minor')))}"
        )

    lines.append("")
    source_kind = str(payload.get("source_kind") or "unknown")
    lines.append(
        "<b>Valuation evidence:</b> "
        f"{html.escape(str(payload.get('source') or 'unknown'))} "
        f"({_int(payload.get('sample_size')) or 0} observation(s), "
        f"{_int(payload.get('window_days')) or 0} day window)"
    )
    lines.append(
        "<b>Evidence type:</b> "
        f"{html.escape(source_kind)}; <b>method:</b> {html.escape(str(payload.get('valuation_method') or 'median'))}"
    )
    lines.append(
        "<b>Confidence:</b> "
        f"{html.escape(str(payload.get('confidence_label') or 'unknown')).title()} "
        f"({_float(payload.get('confidence_score')) * 100:.0f}/100)"
    )
    lines.append(
        "<b>Risk:</b> "
        f"{html.escape(str(payload.get('risk_level') or 'unknown')).title()}"
    )
    lines.append(
        f"<b>Condition:</b> {html.escape(str(payload.get('condition') or 'unknown'))}; "
        f"<b>Marketplace:</b> {html.escape(str(payload.get('marketplace') or 'unknown'))}"
    )

    ship_to = payload.get("ship_to_ok")
    if ship_to is True:
        destination = html.escape(str(payload.get("delivery_country") or "destination"))
        lines.append(f"<b>Delivery:</b> confirmed to {destination}")
    elif ship_to is False:
        lines.append("<b>Delivery:</b> not available to the destination country")
    else:
        destination = html.escape(str(payload.get("delivery_country") or "destination"))
        lines.append(f"<b>Delivery:</b> unconfirmed to {destination}")

    seller = payload.get("seller_name")
    if seller:
        feedback_bits = []
        if payload.get("seller_feedback_pct") is not None:
            feedback_bits.append(f"{_float(payload.get('seller_feedback_pct')):.1f}% positive")
        if payload.get("seller_feedback_score") is not None:
            feedback_bits.append(f"{_int(payload.get('seller_feedback_score'))} ratings")
        feedback = f" ({', '.join(feedback_bits)})" if feedback_bits else ""
        lines.append(f"<b>Seller:</b> {html.escape(str(seller))}{feedback}")
    if payload.get("location_country"):
        lines.append(
            "<b>Item location:</b> "
            f"{html.escape(str(payload.get('location_country')))}"
        )

    if is_auction:
        end_at = _format_timestamp(payload.get("end_at"))
        if end_at:
            lines.append(f"<b>Auction ends:</b> {html.escape(end_at)}")
    lines.append(
        f"<b>First seen:</b> {html.escape(_format_timestamp(payload.get('first_seen_at')) or 'unknown')}"
    )

    components = payload.get("components") or []
    if components:
        lines.append("<b>Bundle components:</b> " + html.escape(", ".join(map(str, components))))
    flags = payload.get("risk_flags") or []
    if flags:
        lines.append("<b>Flags:</b> " + html.escape(", ".join(map(str, flags))))
    notes = payload.get("notes") or []
    if notes:
        lines.append("<b>Watch outs:</b> " + html.escape("; ".join(map(str, notes))))

    lines.append("")
    url = str(payload.get("url") or "")
    if url.startswith("http"):
        lines.append(f'<a href="{html.escape(url, quote=True)}">Open listing on eBay</a>')
    lines.append(
        "<i>Estimate only, not a verified resale value. Check the listing, condition, "
        "seller and delivery before buying.</i>"
    )
    return "\n".join(lines)


def _int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float:
    try:
        return float(value) if value is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _format_timestamp(value: Any) -> str | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
