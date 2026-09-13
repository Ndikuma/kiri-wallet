import uuid

from django.db import transaction
from rest_framework import generics, status
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Transaction, TransactionStatus, TransactionType, Wallet
from .serializers import (
    CreateDepositSerializer,
    CreateWithdrawalSerializer,
    TransactionSerializer,
    WalletSerializer,
)


def get_or_create_wallet(user):
    wallet, _ = Wallet.objects.get_or_create(user=user)
    return wallet


class WalletDetailView(APIView):
    def get(self, request):
        wallet = get_or_create_wallet(request.user)
        return Response(WalletSerializer(wallet).data)


class TransactionListView(generics.ListAPIView):
    serializer_class = TransactionSerializer

    def get_queryset(self):
        wallet = get_or_create_wallet(self.request.user)
        return wallet.transactions.all()


class CreateDepositView(APIView):
    """Create a pending deposit request.

    NOTE: this only records the intent. Wiring this to a real Lightning
    node / on-chain address generator (LND, Blink, etc.) is a follow-up step.
    """

    def post(self, request):
        serializer = CreateDepositSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        wallet = get_or_create_wallet(request.user)
        txn = Transaction.objects.create(
            id=uuid.uuid4(),
            wallet=wallet,
            type=TransactionType.DEPOSIT,
            status=TransactionStatus.PENDING,
            amount_sats=data["amount_sats"],
            memo=data.get("memo", ""),
        )
        return Response(TransactionSerializer(txn).data, status=status.HTTP_201_CREATED)


class CreateWithdrawalView(APIView):
    """Create a pending withdrawal request and lock the funds from the available balance.

    NOTE: actually paying out over Lightning/on-chain is a follow-up step;
    this view only reserves the balance and records the request.
    """

    def post(self, request):
        serializer = CreateWithdrawalSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        amount = data["amount_sats"]

        get_or_create_wallet(request.user)
        with transaction.atomic():
            wallet = Wallet.objects.select_for_update().get(user=request.user)
            if wallet.available_balance < amount:
                return Response(
                    {"detail": "Insufficient balance."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            wallet.available_balance -= amount
            wallet.save(update_fields=["available_balance", "updated_at"])

            txn = Transaction.objects.create(
                id=uuid.uuid4(),
                wallet=wallet,
                type=TransactionType.WITHDRAWAL,
                status=TransactionStatus.PENDING,
                amount_sats=amount,
                balance_after=wallet.available_balance,
                lightning_invoice=data.get("lightning_invoice", ""),
                onchain_address=data.get("onchain_address", ""),
            )

        return Response(TransactionSerializer(txn).data, status=status.HTTP_201_CREATED)
