"""
Receives AmatoPay merchant webhook deliveries (payment.paid, payment.failed,
delivery.confirmed, settlement.completed, ...). See AmatoPay's docs/WEBHOOKS.md.

Configured from AmatoPay's Merchant Dashboard -> Developers, pointed at this
endpoint's absolute URL; the `whsec_...` signing secret goes in
`AMATOPAY_WEBHOOK_SECRET` (settings/.env).
"""
from __future__ import annotations

import json
import logging

from django.http import HttpResponse, HttpResponseBadRequest
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from wallet.amatopay_client import verify_amatopay_signature
from wallet.amatopay_topup import process_webhook_event
from wallet.options import SETTINGS

logger = logging.getLogger(__name__)


@csrf_exempt
@require_POST
def amatopay_webhook(request):
    secret = SETTINGS.AMATOPAY_WEBHOOK_SECRET
    if not secret:
        logger.error("AMATOPAY_WEBHOOK_SECRET is not configured; rejecting webhook delivery.")
        return HttpResponse(status=503)

    signature_header = request.headers.get("AmatoPay-Signature", "")
    if not verify_amatopay_signature(secret, signature_header, request.body):
        logger.warning("Rejected AmatoPay webhook delivery: invalid or missing signature.")
        return HttpResponseBadRequest("invalid signature")

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return HttpResponseBadRequest("invalid payload")

    event_id = payload.get("id", "")
    event_type = payload.get("type", "")
    data = payload.get("data") or {}
    if not event_id or not event_type:
        return HttpResponseBadRequest("missing id/type")

    # Durably stored before we return — processing errors are recorded on the event
    # row, not surfaced here, so a bug in our handling never turns into a webhook
    # retry storm on AmatoPay's side.
    process_webhook_event(event_id=event_id, event_type=event_type, data=data, raw_payload=payload)
    return HttpResponse(status=200)
