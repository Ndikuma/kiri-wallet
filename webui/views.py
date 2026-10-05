from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login as auth_login, logout as auth_logout
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from wallet import bif
from wallet.amatopay_client import AmatoPayError
from wallet.amatopay_topup import check_topup_session, confirm_delivery, create_topup_session, verify_payer_alias
from wallet.bitcoin import CustodialBitcoinService, deposit_confirmations_required
from wallet.blink_wallet import BlinkWallet, BlinkWalletError
from wallet.models import (
    AmatoPayCheckoutSession,
    AmatoPayCheckoutSessionStatus,
    ExchangeRate,
    POSCharge,
    POSChargeStatus,
    POSChargeType,
    TransactionStatus,
    TransactionType,
    Wallet,
)
from wallet.withdrawal import process_withdrawal
from webui.forms import LoginForm, RegisterForm


def _wallet(user) -> Wallet:
    return Wallet.objects.get_or_create(user=user)[0]


# ── Public ───────────────────────────────────────────────────

def landing(request):
    return render(request, "landing.html")


def register_view(request):
    if request.user.is_authenticated:
        return redirect("webui:dashboard")

    if request.method == "POST":
        form = RegisterForm(request.POST)
        if form.is_valid():
            user = form.save()
            _wallet(user)
            auth_login(request, user)
            messages.success(request, "Welcome — your wallet is ready.")
            return redirect("webui:dashboard")
    else:
        form = RegisterForm()

    return render(request, "auth/register.html", {"form": form})


def login_view(request):
    if request.user.is_authenticated:
        return redirect("webui:dashboard")

    next_url = request.POST.get("next") or request.GET.get("next") or ""
    if request.method == "POST":
        form = LoginForm(request, data=request.POST)
        if form.is_valid():
            auth_login(request, form.get_user())
            return redirect(next_url or "webui:dashboard")
    else:
        form = LoginForm(request)

    return render(request, "auth/login.html", {"form": form, "next_url": next_url})


@require_POST
def logout_view(request):
    auth_logout(request)
    return redirect("webui:landing")


# ── Dashboard ────────────────────────────────────────────────

@login_required
def dashboard(request):
    wallet = _wallet(request.user)
    recent_transactions = wallet.transactions.all().order_by("-created_at")[:8]
    rate = ExchangeRate.current()
    return render(request, "wallet/dashboard.html", {
        "wallet": wallet,
        "recent_transactions": recent_transactions,
        "rate": rate,
    })


# ── Deposit (Lightning) ──────────────────────────────────────

@login_required
def deposit_view(request):
    wallet = _wallet(request.user)
    invoice = None

    if request.method == "POST":
        try:
            amount = int(request.POST.get("amount", "0"))
        except ValueError:
            amount = 0
        memo = request.POST.get("memo", "")[:120]

        if amount < 1000:
            messages.error(request, "Minimum deposit is 1,000 sats.")
        else:
            try:
                blink = BlinkWallet()
                invoice = blink.create_ln_invoice(amount, memo=memo or "Wallet deposit")
            except BlinkWalletError as exc:
                messages.error(request, f"Could not create invoice: {exc}")
            except Exception as exc:
                messages.error(request, f"Blink is unavailable right now: {exc}")
            else:
                wallet.add_pending_balance(amount)
                wallet.settle(
                    amount, TransactionType.DEPOSIT,
                    lnd_invoice=invoice.get("paymentRequest", ""),
                    lnd_payment_hash=invoice.get("paymentHash", ""),
                    status=TransactionStatus.PENDING, description=memo,
                    balance_after=wallet.available_balance,
                )

    return render(request, "wallet/deposit.html", {"wallet": wallet, "invoice": invoice})


@login_required
def deposit_status_json(request, payment_hash):
    wallet = _wallet(request.user)
    tx = wallet.transactions.filter(lnd_payment_hash=payment_hash, type=TransactionType.DEPOSIT).first()
    if not tx:
        return JsonResponse({"found": False}, status=404)
    return JsonResponse({
        "found": True, "status": tx.status,
        "available_balance": wallet.available_balance, "pending_balance": wallet.pending_balance,
    })


# ── Withdraw ─────────────────────────────────────────────────

@login_required
def withdraw_view(request):
    wallet = _wallet(request.user)
    result = None

    if request.method == "POST":
        target = request.POST.get("target", "").strip()
        amount_raw = request.POST.get("amount", "").strip()
        amount = int(amount_raw) if amount_raw.isdigit() else None
        memo = request.POST.get("memo", "")[:120]

        try:
            tx, provider_result = process_withdrawal(wallet=wallet, destination=target, amount_sats=amount, memo=memo)
        except ValueError as exc:
            messages.error(request, str(exc))
        except Exception as exc:
            messages.error(request, f"Withdrawal provider error: {exc}")
        else:
            messages.success(request, f"Withdrawal of {tx.amount} sats submitted.")
            result = {"tx": tx, "provider_result": provider_result}

    return render(request, "wallet/withdraw.html", {"wallet": wallet, "result": result})


# ── On-chain Bitcoin address ─────────────────────────────────

@login_required
def bitcoin_address_view(request):
    wallet = _wallet(request.user)

    service = CustodialBitcoinService()
    address_info = None

    if request.method == "POST":
        try:
            if request.POST.get("action") == "new" and wallet.bitcoin_address:
                address_info = service.new_user_address(wallet)
                messages.success(request, "New deposit address generated. Your previous addresses still work.")
            else:
                address_info = service.get_or_create_user_address(wallet)
            wallet.refresh_from_db()
        except Exception as exc:
            messages.error(request, f"Could not generate an address: {exc}")
    elif wallet.bitcoin_address:
        try:
            address_info = service.get_or_create_user_address(wallet)
        except Exception:
            address_info = None

    onchain_transactions = (
        wallet.transactions.exclude(onchain_txid="").exclude(network="lightning").order_by("-created_at")[:10]
    )
    return render(request, "wallet/bitcoin.html", {
        "wallet": wallet,
        "qr_code": address_info["qr"] if address_info else "",
        "address_info": address_info,
        "confirmations_required": deposit_confirmations_required(),
        "network": settings.BITCOIN_NETWORK,
        "onchain_transactions": onchain_transactions,
    })


# ── Transactions ─────────────────────────────────────────────

@login_required
def transactions_view(request):
    wallet = _wallet(request.user)
    qs = wallet.transactions.all().order_by("-created_at")
    paginator = Paginator(qs, 25)
    page = paginator.get_page(request.GET.get("page"))
    return render(request, "wallet/transactions.html", {"wallet": wallet, "page": page})


# ── BIF exchange ─────────────────────────────────────────────

@login_required
def exchange_view(request):
    wallet = _wallet(request.user)
    rate = ExchangeRate.current()

    if request.method == "POST":
        amount_raw = request.POST.get("amount", "").strip()
        amount = int(amount_raw) if amount_raw.isdigit() else 0

        try:
            result = bif.convert_sats_to_bif(wallet, amount)
        except ValueError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, f"Converted {result['amount_sats']} sats and {result['amount_bif']} BIF at {result['rate_bif_per_btc']} BIF/BTC.")
            wallet.refresh_from_db()

    return render(request, "wallet/exchange.html", {"wallet": wallet, "rate": rate})


# ── AmatoPay alias verification (shared: POS BIF charge + top-up) ─────

@login_required
@require_POST
def amatopay_verify_alias_json(request):
    """Step 1 of the checkout flow, called by JS before the form's real submit: look
    up the payer's mobile alias and show its resolved name for confirmation, so the
    checkout session (step 2) is only created once the operator has actually confirmed
    who they're about to charge."""
    payer_alias = request.POST.get("payer_alias", "").strip()
    if not payer_alias:
        return JsonResponse({"ok": False, "error": "Enter a mobile alias."}, status=400)
    try:
        result = verify_payer_alias(payer_alias)
    except AmatoPayError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)
    return JsonResponse({
        "ok": True,
        "payer_alias": result.get("payer_alias", payer_alias),
        "customer_full_name": result.get("customer_full_name", ""),
        "status": result.get("status", ""),
        "currency": result.get("currency", ""),
    })


# ── POS charge ───────────────────────────────────────────────

@login_required
def pos_view(request):
    wallet = _wallet(request.user)
    charge = None
    qr_code = ""
    checkout_url = ""
    charge_type = request.POST.get("charge_type", POSChargeType.SATS_TO_BIF)

    if request.method == "POST":
        memo = request.POST.get("memo", "")[:255]

        try:
            if charge_type == POSChargeType.BIF_TO_SATS:
                amount_bif_raw = request.POST.get("amount_bif", "").strip()
                amount_bif = int(amount_bif_raw) if amount_bif_raw.isdigit() else 0
                payer_alias = request.POST.get("payer_alias", "").strip()
                return_url = request.build_absolute_uri(reverse("webui:pos_return"))
                charge, session = bif.create_pos_charge_bif_to_sats(
                    wallet, amount_bif=amount_bif, payer_alias=payer_alias, memo=memo, return_url=return_url,
                )
                checkout_url = session.checkout_url
            else:
                amount_sats_raw = request.POST.get("amount_sats", "").strip()
                amount_sats = int(amount_sats_raw) if amount_sats_raw.isdigit() else 0
                charge, invoice = bif.create_pos_charge(wallet, amount_sats=amount_sats, memo=memo)
                qr_code = invoice.get("qrCode", "")
        except (ValueError, BlinkWalletError, AmatoPayError) as exc:
            messages.error(request, str(exc))

    recent_charges = wallet.pos_charges.all().order_by("-created_at")[:8]
    return render(request, "wallet/pos.html", {
        "wallet": wallet, "charge": charge, "qr_code": qr_code, "checkout_url": checkout_url,
        "charge_type": charge_type, "recent_charges": recent_charges,
    })


@login_required
def pos_status_json(request, charge_id):
    charge = get_object_or_404(POSCharge, pk=charge_id, wallet=_wallet(request.user))
    if charge.amatopay_session_id and charge.status == POSChargeStatus.PENDING:
        try:
            check_topup_session(charge.amatopay_session)
            charge.refresh_from_db()
        except AmatoPayError:
            pass
    return JsonResponse({
        "status": charge.status, "charge_type": charge.charge_type,
        "amount_sats": charge.amount_sats, "bif_equivalent": charge.bif_equivalent,
    })


def pos_return_view(request):
    """Public landing page AmatoPay redirects a *payer's* browser to after a BIF_TO_SATS
    POS checkout (the payer is a customer paying via mobile money, not a logged-in
    account holder). AmatoPay appends `?payment_reference=...` — we reconcile against
    AmatoPay's authoritative status rather than trusting the redirect itself."""
    payment_reference = request.GET.get("payment_reference", "")
    session = AmatoPayCheckoutSession.objects.filter(payment_reference=payment_reference).first() if payment_reference else None

    if session:
        try:
            session = check_topup_session(session)
        except AmatoPayError:
            pass

    return render(request, "wallet/pos_return.html", {"session": session})


@login_required
@require_POST
def pos_confirm_delivery_view(request, charge_id):
    """The merchant asks the customer for the six-digit release code AmatoPay sent to
    their phone and enters it here, so AmatoPay releases the held funds to us."""
    charge = get_object_or_404(POSCharge, pk=charge_id, wallet=_wallet(request.user))
    secure_code = request.POST.get("secure_code", "").strip()

    if not charge.amatopay_session_id:
        messages.error(request, "This charge has no AmatoPay payment to confirm.")
    else:
        try:
            confirm_delivery(charge.amatopay_session, secure_code)
        except AmatoPayError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, "Release code accepted — AmatoPay is settling the funds to us.")

    return redirect("webui:pos")


# ── AmatoPay BIF top-up ──────────────────────────────────────

@login_required
def topup_view(request):
    wallet = _wallet(request.user)
    session = None

    if request.method == "POST":
        try:
            amount_bif = int(request.POST.get("amount_bif", "0"))
        except ValueError:
            amount_bif = 0
        payer_alias = request.POST.get("payer_alias", "").strip()

        try:
            return_url = request.build_absolute_uri(reverse("webui:topup_return"))
            session = create_topup_session(wallet, amount_bif=amount_bif, payer_alias=payer_alias, return_url=return_url)
        except (ValueError, AmatoPayError) as exc:
            messages.error(request, str(exc))
        else:
            # Step 3: hand the payer's browser to AmatoPay's hosted checkout page.
            return redirect(session.checkout_url)
    else:
        session_id = request.GET.get("session", "")
        if session_id:
            session = AmatoPayCheckoutSession.objects.filter(session_id=session_id, wallet=wallet).first()

    recent_sessions = wallet.amatopay_sessions.all().order_by("-created_at")[:8]
    return render(request, "wallet/topup.html", {"wallet": wallet, "session": session, "recent_sessions": recent_sessions})


@login_required
def topup_return_view(request):
    """AmatoPay redirects the payer's browser back here (`return_url`) with
    `?payment_reference=...` once the checkout reaches a terminal outcome. This is UX
    only — we re-query AmatoPay for the authoritative status before crediting anything."""
    wallet = _wallet(request.user)
    payment_reference = request.GET.get("payment_reference", "")
    session = AmatoPayCheckoutSession.objects.filter(wallet=wallet, payment_reference=payment_reference).first() if payment_reference else None

    if not session:
        messages.warning(request, "We couldn't find that top-up session.")
        return redirect("webui:topup")

    try:
        session = check_topup_session(session)
    except AmatoPayError:
        pass

    if session.status == AmatoPayCheckoutSessionStatus.CONFIRMED:
        if session.awaiting_delivery_confirmation:
            messages.success(request, f"Top-up of {session.amount_bif} BIF confirmed — enter the release code AmatoPay sent you below to finish releasing the funds to us.")
        else:
            messages.success(request, f"Top-up of {session.amount_bif} BIF confirmed.")
    elif session.status == AmatoPayCheckoutSessionStatus.FAILED:
        messages.error(request, "The top-up payment failed or was cancelled.")
    else:
        messages.info(request, "Your top-up is still processing — we'll confirm it as soon as AmatoPay reports it collected.")

    return redirect(f"{reverse('webui:topup')}?session={session.session_id}")


@login_required
@require_POST
def topup_confirm_delivery_view(request, session_id):
    """You (the payer) read AmatoPay's six-digit release code off your own phone and
    enter it here so AmatoPay releases the held funds to our settlement account."""
    session = get_object_or_404(AmatoPayCheckoutSession, session_id=session_id, wallet=_wallet(request.user))
    secure_code = request.POST.get("secure_code", "").strip()

    try:
        confirm_delivery(session, secure_code)
    except AmatoPayError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Release code accepted — AmatoPay is settling the funds to us.")

    return redirect(f"{reverse('webui:topup')}?session={session.session_id}")


@login_required
def topup_status_json(request, session_id):
    session = get_object_or_404(AmatoPayCheckoutSession, session_id=session_id, wallet=_wallet(request.user))
    try:
        session = check_topup_session(session)
    except AmatoPayError as exc:
        return JsonResponse({"status": session.status, "error": str(exc)})
    return JsonResponse({"status": session.status})
