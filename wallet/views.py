from rest_framework import status as http_status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from wallet import bif
from wallet.amatopay_client import AmatoPayError
from wallet.amatopay_topup import check_topup_session, confirm_delivery, create_topup_session, verify_payer_alias
from wallet.bitcoin import CustodialBitcoinService
from wallet.blink_wallet import BlinkWallet, BlinkWalletError
from wallet.models import (
    AmatoPayCheckoutSession,
    ExchangeRate,
    POSCharge,
    POSChargeStatus,
    TransactionStatus,
    TransactionType,
    Wallet,
    WalletTransaction,
)
from wallet.options import SETTINGS
from wallet.provider_status import get_amatopay_status, get_blink_status, get_onchain_status
from wallet.serializers import (
    AmatoPayConfirmDeliverySerializer,
    AmatoPayTopupCreateSerializer,
    AmatoPayTopupSessionSerializer,
    AmatoPayVerifyAliasSerializer,
    DepositRequestSerializer,
    ExchangeConvertSerializer,
    ExchangeQuoteSerializer,
    ExchangeRateSerializer,
    POSChargeBifCreateSerializer,
    POSChargeCreateSerializer,
    POSChargeSerializer,
    WalletSerializer,
    WalletTransactionSerializer,
    WithdrawalDecodeSerializer,
    WithdrawalFeeEstimateSerializer,
    WithdrawalRequestSerializer,
)
from wallet.withdrawal import decode_withdrawal_target, estimate_withdrawal_fees, process_withdrawal


class WalletViewSet(viewsets.GenericViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = WalletSerializer

    def _wallet(self) -> Wallet:
        return Wallet.objects.get_or_create(user=self.request.user)[0]

    # ── core wallet ─────────────────────────────────────────

    @action(detail=False, methods=["GET"])
    def me(self, request):
        return Response({"success": True, "data": WalletSerializer(self._wallet()).data})

    @action(detail=False, methods=["GET"])
    def transactions(self, request):
        wallet = self._wallet()
        qs = wallet.transactions.all().order_by("-created_at")
        return Response({"success": True, "data": WalletTransactionSerializer(qs, many=True).data})

    # ── Lightning deposit / withdrawal (Blink) ─────────────

    @action(detail=False, methods=["POST"])
    def deposit(self, request):
        wallet = self._wallet()
        serializer = DepositRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        amount = serializer.validated_data["amount"]

        try:
            blink = BlinkWallet()
            invoice = blink.create_ln_invoice(amount, memo=serializer.validated_data.get("memo", ""))
        except BlinkWalletError as exc:
            return Response({"success": False, "errors": [{"field": "amount", "message": str(exc)}]}, status=http_status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            return Response({"success": False, "errors": [{"field": "amount", "message": f"Blink API error: {exc}"}]}, status=http_status.HTTP_502_BAD_GATEWAY)

        wallet.add_pending_balance(amount)
        tx = wallet.settle(
            amount, TransactionType.DEPOSIT,
            lnd_invoice=invoice.get("paymentRequest", ""), lnd_payment_hash=invoice.get("paymentHash", ""),
            status=TransactionStatus.PENDING, description=serializer.validated_data.get("memo", ""),
            balance_after=wallet.available_balance,
        )
        return Response({
            "success": True,
            "message": "Invoice generated successfully.",
            "data": {
                "transaction_id": str(tx.id),
                "payment_request": invoice.get("paymentRequest", ""),
                "payment_hash": invoice.get("paymentHash", ""),
                "amount_sats": amount,
                "expires_at": invoice.get("expiresAt"),
                "qr_code": invoice.get("qrCode", ""),
                "pending_balance": wallet.pending_balance,
                "available_balance": wallet.available_balance,
            },
        })

    @action(detail=False, methods=["GET"])
    def deposit_status(self, request):
        payment_hash = request.query_params.get("payment_hash", "")
        if not payment_hash:
            return Response({"success": False, "message": "payment_hash query param is required."}, status=http_status.HTTP_400_BAD_REQUEST)

        try:
            tx = WalletTransaction.objects.select_related("wallet", "user").get(
                lnd_payment_hash=payment_hash, type=TransactionType.DEPOSIT, user=request.user,
            )
        except WalletTransaction.DoesNotExist:
            return Response({"success": False, "message": "Deposit transaction not found."}, status=http_status.HTTP_404_NOT_FOUND)

        blink_info = {}
        if SETTINGS.has_blink:
            try:
                blink_info = BlinkWallet().get_ln_invoice_status(payment_hash=payment_hash)
            except BlinkWalletError:
                pass

        return Response({
            "success": True,
            "data": {
                "transaction_id": str(tx.id), "payment_hash": tx.lnd_payment_hash, "payment_request": tx.lnd_invoice,
                "amount_sats": tx.amount, "blink_status": blink_info.get("status", tx.status), "status": tx.status,
                "balance_after": tx.balance_after, "pending_balance": tx.wallet.pending_balance,
                "available_balance": tx.wallet.available_balance, "created_at": tx.created_at, "settled_at": tx.settled_at,
            },
        })

    @action(detail=False, methods=["POST"])
    def decode_withdrawal(self, request):
        serializer = WithdrawalDecodeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            decoded = decode_withdrawal_target(serializer.validated_data["target"])
        except Exception as exc:
            return Response({"success": False, "errors": [{"field": "target", "message": str(exc)}]}, status=http_status.HTTP_400_BAD_REQUEST)
        return Response({"success": True, "data": decoded.as_dict()})

    @action(detail=False, methods=["POST"])
    def withdrawal_fees(self, request):
        wallet = self._wallet()
        serializer = WithdrawalFeeEstimateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            estimate = estimate_withdrawal_fees(wallet=wallet, destination=serializer.validated_data["target"], amount_sats=serializer.validated_data.get("amount"))
        except Exception as exc:
            return Response({"success": False, "errors": [{"field": "target", "message": str(exc)}]}, status=http_status.HTTP_400_BAD_REQUEST)
        return Response({"success": True, "data": estimate})

    @action(detail=False, methods=["POST"])
    def withdraw(self, request):
        wallet = self._wallet()
        serializer = WithdrawalRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            tx, provider_result = process_withdrawal(
                wallet=wallet, destination=serializer.validated_data["target"],
                amount_sats=serializer.validated_data.get("amount"), memo=serializer.validated_data.get("memo", ""),
            )
        except ValueError as exc:
            return Response({"success": False, "errors": [{"field": "withdrawal", "message": str(exc)}]}, status=http_status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            return Response({"success": False, "errors": [{"field": "provider", "message": str(exc)}]}, status=http_status.HTTP_502_BAD_GATEWAY)
        return Response({
            "success": True, "message": "Withdrawal submitted.",
            "data": {
                "txn_id": str(tx.id), "amount_sats": tx.amount, "status": tx.status,
                "balance_after": tx.balance_after, "available_balance": wallet.available_balance,
                "withdrawal": provider_result,
            },
        })

    # ── on-chain Bitcoin (optional; requires bitcoinlib) ────

    @action(detail=False, methods=["GET"])
    def my_bitcoin_address(self, request):
        wallet = self._wallet()
        if not wallet.bitcoin_address:
            return Response({"success": False, "message": "No deposit address generated yet. Call generate_deposit_address first."}, status=http_status.HTTP_404_NOT_FOUND)
        try:
            service = CustodialBitcoinService()
            qr = service.generate_qr(wallet.bitcoin_address)
        except Exception as exc:
            return Response({"success": False, "errors": [{"field": "address", "message": str(exc)}]}, status=http_status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response({"success": True, "data": {"bitcoin_address": wallet.bitcoin_address, "qr_code": qr}})

    @action(detail=False, methods=["POST"])
    def generate_deposit_address(self, request):
        wallet = self._wallet()
        try:
            service = CustodialBitcoinService()
            result = service.get_or_create_user_address(wallet)
        except Exception as exc:
            return Response({"success": False, "errors": [{"field": "address", "message": str(exc)}]}, status=http_status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response({"success": True, "data": result, "message": "Deposit address generated."})

    # ── provider status ─────────────────────────────────────

    @action(detail=False, methods=["GET"])
    def blink_status(self, request):
        status = get_blink_status()
        if not status["success"]:
            code = http_status.HTTP_503_SERVICE_UNAVAILABLE if not status["configured"] else http_status.HTTP_502_BAD_GATEWAY
            return Response({"success": False, "errors": [{"field": "blink", "message": status["message"]}], "data": status}, status=code)
        return Response({"success": True, "data": status})

    @action(detail=False, methods=["GET"])
    def onchain_status(self, request):
        status = get_onchain_status()
        code = http_status.HTTP_200_OK if status["success"] else http_status.HTTP_502_BAD_GATEWAY
        return Response({"success": status["success"], "data": status}, status=code)

    @action(detail=False, methods=["GET"])
    def amatopay_status(self, request):
        status = get_amatopay_status()
        code = http_status.HTTP_200_OK if status["success"] else http_status.HTTP_503_SERVICE_UNAVAILABLE
        return Response({"success": status["success"], "data": status}, status=code)

    # ── BIF exchange (internal ledger, sats <-> BIF) ───────

    @action(detail=False, methods=["GET"], url_path="exchange/rate")
    def exchange_rate(self, request):
        return Response({"success": True, "data": ExchangeRateSerializer(ExchangeRate.current()).data})

    @action(detail=False, methods=["POST"], url_path="exchange/quote")
    def exchange_quote(self, request):
        serializer = ExchangeQuoteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            quote = bif.get_quote(amount_sats=serializer.validated_data["amount_sats"])
        except ValueError as exc:
            return Response({"success": False, "errors": [{"field": "amount_sats", "message": str(exc)}]}, status=http_status.HTTP_400_BAD_REQUEST)
        return Response({"success": True, "data": quote})

    @action(detail=False, methods=["POST"], url_path="exchange/convert")
    def exchange_convert(self, request):
        wallet = self._wallet()
        serializer = ExchangeConvertSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            result = bif.convert_sats_to_bif(wallet, serializer.validated_data["amount_sats"])
        except ValueError as exc:
            return Response({"success": False, "errors": [{"field": "amount_sats", "message": str(exc)}]}, status=http_status.HTTP_400_BAD_REQUEST)
        return Response({"success": True, "message": "Converted.", "data": result})

    # ── POS: quote a charge in sats, settle as BIF ─────────

    @action(detail=False, methods=["POST"], url_path="pos/charge")
    def pos_charge(self, request):
        wallet = self._wallet()
        serializer = POSChargeCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            pos_charge, invoice = bif.create_pos_charge(wallet, **serializer.validated_data)
        except BlinkWalletError as exc:
            return Response({"success": False, "errors": [{"field": "amount_sats", "message": str(exc)}]}, status=http_status.HTTP_400_BAD_REQUEST)
        except ValueError as exc:
            return Response({"success": False, "errors": [{"field": "amount_sats", "message": str(exc)}]}, status=http_status.HTTP_400_BAD_REQUEST)
        data = POSChargeSerializer(pos_charge).data
        data["qr_code"] = invoice.get("qrCode", "")
        return Response({"success": True, "message": "POS charge created.", "data": data}, status=http_status.HTTP_201_CREATED)

    @action(detail=False, methods=["POST"], url_path="pos/charge/bif")
    def pos_charge_bif(self, request):
        wallet = self._wallet()
        serializer = POSChargeBifCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            pos_charge, session = bif.create_pos_charge_bif_to_sats(wallet, **serializer.validated_data)
        except AmatoPayError as exc:
            return Response({"success": False, "errors": [{"field": "amount_bif", "message": str(exc)}]}, status=http_status.HTTP_502_BAD_GATEWAY)
        except ValueError as exc:
            return Response({"success": False, "errors": [{"field": "amount_bif", "message": str(exc)}]}, status=http_status.HTTP_400_BAD_REQUEST)
        return Response({"success": True, "message": "POS charge created.", "data": POSChargeSerializer(pos_charge).data}, status=http_status.HTTP_201_CREATED)

    @action(detail=False, methods=["GET"], url_path=r"pos/charge/(?P<pos_charge_id>[^/.]+)")
    def pos_charge_status(self, request, pos_charge_id=None):
        try:
            pos_charge = POSCharge.objects.get(pk=pos_charge_id, wallet=self._wallet())
        except POSCharge.DoesNotExist:
            return Response({"success": False, "message": "POS charge not found."}, status=http_status.HTTP_404_NOT_FOUND)
        if pos_charge.amatopay_session_id and pos_charge.status == POSChargeStatus.PENDING:
            try:
                check_topup_session(pos_charge.amatopay_session)
                pos_charge.refresh_from_db()
            except AmatoPayError:
                pass
        return Response({"success": True, "data": POSChargeSerializer(pos_charge).data})

    @action(detail=False, methods=["POST"], url_path=r"pos/charge/(?P<pos_charge_id>[^/.]+)/confirm-delivery")
    def pos_charge_confirm_delivery(self, request, pos_charge_id=None):
        try:
            pos_charge = POSCharge.objects.get(pk=pos_charge_id, wallet=self._wallet())
        except POSCharge.DoesNotExist:
            return Response({"success": False, "message": "POS charge not found."}, status=http_status.HTTP_404_NOT_FOUND)
        if not pos_charge.amatopay_session_id:
            return Response({"success": False, "message": "This charge has no AmatoPay payment to confirm."}, status=http_status.HTTP_400_BAD_REQUEST)
        serializer = AmatoPayConfirmDeliverySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            confirm_delivery(pos_charge.amatopay_session, serializer.validated_data["secure_code"])
        except AmatoPayError as exc:
            return Response({"success": False, "errors": [{"field": "secure_code", "message": str(exc)}]}, status=http_status.HTTP_400_BAD_REQUEST)
        return Response({"success": True, "message": "Delivery confirmed.", "data": POSChargeSerializer(pos_charge).data})

    # ── AmatoPay: alias verification (shared: POS BIF charge + top-up) ────

    @action(detail=False, methods=["POST"], url_path="amatopay/verify-alias")
    def amatopay_verify_alias(self, request):
        serializer = AmatoPayVerifyAliasSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            result = verify_payer_alias(serializer.validated_data["payer_alias"])
        except AmatoPayError as exc:
            return Response({"success": False, "errors": [{"field": "payer_alias", "message": str(exc)}]}, status=http_status.HTTP_400_BAD_REQUEST)
        return Response({"success": True, "data": result})

    # ── AmatoPay BIF top-up (mobile-money collection) ──────

    @action(detail=False, methods=["POST"], url_path="bif/topup")
    def bif_topup(self, request):
        wallet = self._wallet()
        serializer = AmatoPayTopupCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            session = create_topup_session(wallet, **serializer.validated_data)
        except AmatoPayError as exc:
            return Response({"success": False, "errors": [{"field": "amatopay", "message": str(exc)}]}, status=http_status.HTTP_502_BAD_GATEWAY)
        except ValueError as exc:
            return Response({"success": False, "errors": [{"field": "amount_bif", "message": str(exc)}]}, status=http_status.HTTP_400_BAD_REQUEST)
        return Response({"success": True, "message": "Top-up session created.", "data": AmatoPayTopupSessionSerializer(session).data}, status=http_status.HTTP_201_CREATED)

    @action(detail=False, methods=["GET"], url_path=r"bif/topup/(?P<session_id>[^/.]+)")
    def bif_topup_status(self, request, session_id=None):
        wallet = self._wallet()
        try:
            session = AmatoPayCheckoutSession.objects.get(session_id=session_id, wallet=wallet)
        except AmatoPayCheckoutSession.DoesNotExist:
            return Response({"success": False, "message": "Top-up session not found."}, status=http_status.HTTP_404_NOT_FOUND)
        try:
            session = check_topup_session(session)
        except AmatoPayError as exc:
            return Response({"success": False, "errors": [{"field": "amatopay", "message": str(exc)}]}, status=http_status.HTTP_502_BAD_GATEWAY)
        return Response({"success": True, "data": AmatoPayTopupSessionSerializer(session).data})

    @action(detail=False, methods=["POST"], url_path=r"bif/topup/(?P<session_id>[^/.]+)/confirm-delivery")
    def bif_topup_confirm_delivery(self, request, session_id=None):
        wallet = self._wallet()
        try:
            session = AmatoPayCheckoutSession.objects.get(session_id=session_id, wallet=wallet)
        except AmatoPayCheckoutSession.DoesNotExist:
            return Response({"success": False, "message": "Top-up session not found."}, status=http_status.HTTP_404_NOT_FOUND)
        serializer = AmatoPayConfirmDeliverySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            session = confirm_delivery(session, serializer.validated_data["secure_code"])
        except AmatoPayError as exc:
            return Response({"success": False, "errors": [{"field": "secure_code", "message": str(exc)}]}, status=http_status.HTTP_400_BAD_REQUEST)
        return Response({"success": True, "message": "Delivery confirmed.", "data": AmatoPayTopupSessionSerializer(session).data})
