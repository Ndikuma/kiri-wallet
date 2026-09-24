from datetime import datetime, time, timedelta

from django.conf import settings as django_settings
from django.db.models import Count, Sum
from django.db.models.functions import TruncDate
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from wallet.models import (
    AmatoPayCheckoutSession,
    AmatoPayCheckoutSessionStatus,
    PlatformBitcoinAddress,
    POSCharge,
    POSChargeStatus,
    TransactionCurrency,
    TransactionStatus,
    TransactionType,
    Wallet,
    WalletTransaction,
)
from wallet.options import SETTINGS


def _sats(value):
    return f"{value or 0:,.0f} sats"


def _bif(value):
    return f"{value or 0:,.0f} BIF"


def _admin_url(name):
    try:
        return reverse(name)
    except NoReverseMatch:
        return "#"


def _short_number(value):
    value = value or 0
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.1f}B"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.0f}K"
    return f"{value:.0f}"


def dashboard_callback(request, context):
    now = timezone.now()
    today = timezone.localdate()
    start_of_day = timezone.make_aware(datetime.combine(today, time.min))
    period_start = start_of_day - timedelta(days=29)
    chart_start = today - timedelta(days=13)

    deposits = WalletTransaction.objects.filter(
        type=TransactionType.DEPOSIT, currency=TransactionCurrency.SATS,
    )
    confirmed_deposits = deposits.filter(status=TransactionStatus.CONFIRMED)

    today_totals = confirmed_deposits.filter(settled_at__gte=start_of_day).aggregate(
        count=Count("id"), amount=Sum("amount"),
    )
    period_totals = confirmed_deposits.filter(settled_at__gte=period_start).aggregate(
        count=Count("id"), amount=Sum("amount"),
    )
    pending_deposits = deposits.filter(status=TransactionStatus.PENDING).aggregate(
        count=Count("id"), amount=Sum("amount"),
    )

    wallet_totals = Wallet.objects.filter(is_platform=False).aggregate(
        count=Count("id"),
        sats=Sum("available_balance"),
        pending_sats=Sum("pending_balance"),
        bif=Sum("bif_balance"),
    )

    pos_paid_today = POSCharge.objects.filter(
        status=POSChargeStatus.PAID, paid_at__gte=start_of_day,
    ).aggregate(count=Count("id"), bif=Sum("bif_equivalent"))
    pos_pending = POSCharge.objects.filter(status=POSChargeStatus.PENDING).count()

    topup_pending = AmatoPayCheckoutSession.objects.filter(
        status=AmatoPayCheckoutSessionStatus.PENDING,
    ).count()
    topup_confirmed_period = AmatoPayCheckoutSession.objects.filter(
        status=AmatoPayCheckoutSessionStatus.CONFIRMED, confirmed_at__gte=period_start,
    ).aggregate(count=Count("id"), bif=Sum("amount_bif"))

    onchain_addresses = PlatformBitcoinAddress.objects.count()

    daily_rows = {
        row["day"]: row
        for row in confirmed_deposits.filter(settled_at__date__gte=chart_start)
        .annotate(day=TruncDate("settled_at"))
        .values("day")
        .annotate(volume=Sum("amount"), count=Count("id"))
        .order_by("day")
    }
    chart_max = max((row["volume"] or 0 for row in daily_rows.values()), default=0)
    volume_chart = []
    for offset in range(14):
        day = chart_start + timedelta(days=offset)
        row = daily_rows.get(day, {})
        volume = row.get("volume") or 0
        volume_chart.append({
            "label": day.strftime("%d %b"),
            "short_label": day.strftime("%d"),
            "volume": _sats(volume),
            "short_volume": _short_number(volume),
            "count": row.get("count", 0),
            "height": round(volume / chart_max * 100, 1) if chart_max else 0,
        })

    recent_transactions = WalletTransaction.objects.filter(
        created_at__gte=period_start,
    )
    status_groups = [
        ("Confirmed", [TransactionStatus.CONFIRMED], "green"),
        ("Pending", [TransactionStatus.PENDING], "amber"),
        ("Failed / expired", [TransactionStatus.FAILED, TransactionStatus.EXPIRED], "red"),
        ("Refunded", [TransactionStatus.REFUNDED], "blue"),
    ]
    status_counts = [
        {"label": label, "count": recent_transactions.filter(status__in=statuses).count(), "tone": tone}
        for label, statuses, tone in status_groups
    ]
    status_total = sum(item["count"] for item in status_counts)
    for item in status_counts:
        item["percentage"] = round(item["count"] / status_total * 100) if status_total else 0

    context.update({
        "dashboard_generated_at": now,
        "dashboard_greeting": (
            "morning" if timezone.localtime(now).hour < 12
            else "afternoon" if timezone.localtime(now).hour < 18
            else "evening"
        ),
        "provider_status": {
            "lightning": {"healthy": SETTINGS.has_blink, "url": _admin_url("admin:wallet_wallettransaction_changelist")},
            "amatopay": {"healthy": SETTINGS.has_amatopay, "url": _admin_url("admin:wallet_amatopaycheckoutsession_changelist")},
            "network": django_settings.BITCOIN_NETWORK,
        },
        "stat_cards": [
            {
                "label": "Deposits today",
                "value": today_totals["count"] or 0,
                "detail": _sats(today_totals["amount"]),
                "icon": "call_received",
                "tone": "green",
                "url": _admin_url("admin:wallet_wallettransaction_changelist"),
            },
            {
                "label": "Sats in circulation",
                "value": _sats(wallet_totals["sats"]),
                "detail": f'{wallet_totals["count"] or 0} wallets',
                "icon": "currency_bitcoin",
                "tone": "amber",
                "url": _admin_url("admin:wallet_wallet_changelist"),
            },
            {
                "label": "BIF balance",
                "value": _bif(wallet_totals["bif"]),
                "detail": "Across all wallets",
                "icon": "payments",
                "tone": "blue",
                "url": _admin_url("admin:wallet_wallet_changelist"),
            },
            {
                "label": "Pending deposits",
                "value": pending_deposits["count"] or 0,
                "detail": _sats(pending_deposits["amount"]),
                "icon": "hourglass_top",
                "tone": "purple",
                "url": _admin_url("admin:wallet_wallettransaction_changelist"),
            },
        ],
        "period_summary": {
            "deposits": period_totals["count"] or 0,
            "volume": _sats(period_totals["amount"]),
            "pos_paid": pos_paid_today["count"] or 0,
            "pos_bif": _bif(pos_paid_today["bif"]),
        },
        "volume_chart": volume_chart,
        "status_breakdown": status_counts,
        "status_total": status_total,
        "operations": [
            {
                "label": "Pending deposits",
                "value": pending_deposits["count"] or 0,
                "detail": "Awaiting Lightning settlement or confirmations",
                "url": _admin_url("admin:wallet_wallettransaction_changelist"),
            },
            {
                "label": "POS charges pending",
                "value": pos_pending,
                "detail": "Invoices quoted, awaiting payment",
                "url": _admin_url("admin:wallet_poscharge_changelist"),
            },
            {
                "label": "Mobile-money top-ups pending",
                "value": topup_pending,
                "detail": "Awaiting mobile-money collection",
                "url": _admin_url("admin:wallet_amatopaycheckoutsession_changelist"),
            },
            {
                "label": "On-chain addresses",
                "value": onchain_addresses,
                "detail": "Derived from the platform HD wallet",
                "url": _admin_url("admin:wallet_platformbitcoinaddress_changelist"),
            },
            {
                "label": "Mobile-money top-ups (30d)",
                "value": topup_confirmed_period["count"] or 0,
                "detail": _bif(topup_confirmed_period["bif"]),
                "url": _admin_url("admin:wallet_amatopaycheckoutsession_changelist"),
            },
        ],
        "recent_transactions": WalletTransaction.objects.select_related("user", "wallet")
        .order_by("-created_at")[:8],
    })
    return context
