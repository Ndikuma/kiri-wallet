from django.contrib import messages
from django.contrib.auth import login as auth_login, logout as auth_logout
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from wallet import bif
from wallet.amatopay_client import AmatoPayError
from wallet.amatopay_topup import check_topup_session, create_topup_session
from wallet.bitcoin import CustodialBitcoinService
from wallet.blink_wallet import BlinkWallet, BlinkWalletError
from wallet.models import AmatoPayCheckoutSession, ExchangeRate, POSCharge, TransactionStatus, TransactionType, Wallet
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
    qr_code = ""

    if request.method == "POST":
        try:
            data = CustodialBitcoinService().get_or_create_user_address(wallet)
            wallet.refresh_from_db()
            qr_code = data["qr"]
        except Exception as exc:
            messages.error(request, f"Could not generate an address: {exc}")
    elif wallet.bitcoin_address:
        try:
            qr_code = CustodialBitcoinService().generate_qr(wallet.bitcoin_address)
        except Exception:
            qr_code = ""

    onchain_transactions = wallet.transactions.exclude(onchain_address="").order_by("-created_at")[:10]
    return render(request, "wallet/bitcoin.html", {
        "wallet": wallet, "qr_code": qr_code, "onchain_transactions": onchain_transactions,
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
        direction = request.POST.get("direction")
        amount_raw = request.POST.get("amount", "").strip()
        amount = int(amount_raw) if amount_raw.isdigit() else 0

        try:
            if direction == "sats_to_bif":
                result = bif.convert_sats_to_bif(wallet, amount)
            elif direction == "bif_to_sats":
                result = bif.convert_bif_to_sats(wallet, amount)
            else:
                raise ValueError("Choose a direction.")
        except ValueError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(
                request,
                f"Converted {result['amount_sats']} sats and {result['amount_bif']} BIF at "
                f"{result['rate_bif_per_btc']} BIF/BTC.",
            )
            wallet.refresh_from_db()

    return render(request, "wallet/exchange.html", {"wallet": wallet, "rate": rate})


# ── POS charge ───────────────────────────────────────────────

@login_required
def pos_view(request):
    wallet = _wallet(request.user)
    charge = None
    qr_code = ""

    if request.method == "POST":
        try:
            amount = int(request.POST.get("amount_sats", "0"))
        except ValueError:
            amount = 0
        memo = request.POST.get("memo", "")[:255]

        try:
            charge, invoice = bif.create_pos_charge(wallet, amount_sats=amount, memo=memo)
        except (ValueError, BlinkWalletError) as exc:
            messages.error(request, str(exc))
        else:
            qr_code = invoice.get("qrCode", "")

    recent_charges = wallet.pos_charges.all().order_by("-created_at")[:8]
    return render(request, "wallet/pos.html", {
        "wallet": wallet, "charge": charge, "qr_code": qr_code, "recent_charges": recent_charges,
    })


@login_required
def pos_status_json(request, charge_id):
    charge = get_object_or_404(POSCharge, pk=charge_id, wallet=_wallet(request.user))
    return JsonResponse({"status": charge.status, "bif_equivalent": charge.bif_equivalent})


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
            session = create_topup_session(wallet, amount_bif=amount_bif, payer_alias=payer_alias)
        except (ValueError, AmatoPayError) as exc:
            messages.error(request, str(exc))

    recent_sessions = wallet.amatopay_sessions.all().order_by("-created_at")[:8]
    return render(request, "wallet/topup.html", {"wallet": wallet, "session": session, "recent_sessions": recent_sessions})


@login_required
def topup_status_json(request, session_id):
    session = get_object_or_404(AmatoPayCheckoutSession, session_id=session_id, wallet=_wallet(request.user))
    try:
        session = check_topup_session(session)
    except AmatoPayError as exc:
        return JsonResponse({"status": session.status, "error": str(exc)})
    return JsonResponse({"status": session.status})
