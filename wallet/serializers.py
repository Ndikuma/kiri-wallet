from rest_framework import serializers

from wallet.models import AmatoPayCheckoutSession, ExchangeRate, POSCharge, Wallet, WalletTransaction


class WalletTransactionSerializer(serializers.ModelSerializer):
    type_display = serializers.CharField(source="get_type_display", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = WalletTransaction
        fields = [
            "id", "type", "type_display", "currency", "amount", "balance_after",
            "lnd_invoice", "lnd_payment_hash", "status", "status_display",
            "onchain_address", "onchain_txid", "onchain_vout", "confirmations", "network", "network_fee_sats",
            "description", "linked_object_type", "linked_object_id",
            "created_at", "settled_at",
        ]
        read_only_fields = fields


class WalletSerializer(serializers.ModelSerializer):
    total_sats = serializers.SerializerMethodField()
    bitcoin_qr = serializers.SerializerMethodField()

    class Meta:
        model = Wallet
        fields = [
            "id", "is_platform", "available_balance", "pending_balance", "locked_balance",
            "bif_balance", "total_deposited", "total_withdrawn", "total_sats",
            "bitcoin_address", "bitcoin_qr", "created_at", "updated_at",
        ]
        read_only_fields = fields

    def get_total_sats(self, wallet) -> int:
        return wallet.total_sats()

    def get_bitcoin_qr(self, wallet) -> str:
        if not wallet.bitcoin_address:
            return ""
        from wallet.bitcoin import CustodialBitcoinService
        try:
            return CustodialBitcoinService().generate_qr(wallet.bitcoin_address)
        except Exception:
            return ""


class DepositRequestSerializer(serializers.Serializer):
    amount = serializers.IntegerField(min_value=1_000, help_text="Satoshis (min 1,000)")
    memo = serializers.CharField(required=False, allow_blank=True, max_length=120, default="Wallet deposit")
    expires_in = serializers.IntegerField(required=False, min_value=60, max_value=86400, default=3600)

    def validate_amount(self, value):
        if value % 1000 != 0:
            raise serializers.ValidationError("Deposit amount must be a whole number of SAT.")
        return value


class WithdrawalRequestSerializer(serializers.Serializer):
    amount = serializers.IntegerField(required=False, min_value=1_000, help_text="Satoshis (min 1,000)")
    target = serializers.CharField(max_length=500, required=False, allow_blank=True,
                                    help_text="Lightning invoice, Lightning address, LNURL, or Bitcoin address.")
    memo = serializers.CharField(required=False, allow_blank=True, max_length=120, default="Wallet withdrawal")

    def validate(self, attrs):
        attrs["target"] = (attrs.get("target") or "").strip()
        if not attrs["target"]:
            raise serializers.ValidationError({"target": "Withdrawal destination is required."})
        return attrs


class WithdrawalDecodeSerializer(serializers.Serializer):
    target = serializers.CharField(max_length=500)


class WithdrawalFeeEstimateSerializer(serializers.Serializer):
    amount = serializers.IntegerField(required=False, min_value=1, help_text="Satoshis")
    target = serializers.CharField(max_length=500)


class ExchangeRateSerializer(serializers.ModelSerializer):
    class Meta:
        model = ExchangeRate
        fields = ["id", "bif_per_btc", "source", "is_active", "created_at"]
        read_only_fields = fields


class ExchangeQuoteSerializer(serializers.Serializer):
    amount_sats = serializers.IntegerField(min_value=1, help_text="Satoshis to quote a BIF equivalent for.")


class ExchangeConvertSerializer(serializers.Serializer):
    amount_sats = serializers.IntegerField(min_value=1, help_text="Sats to exchange for BIF.")


class POSChargeSerializer(serializers.ModelSerializer):
    checkout_url = serializers.SerializerMethodField()
    awaiting_delivery_confirmation = serializers.SerializerMethodField()

    class Meta:
        model = POSCharge
        fields = [
            "id", "charge_type", "amount_sats", "bif_equivalent", "rate_bif_per_btc",
            "payment_request", "payer_alias", "checkout_url", "awaiting_delivery_confirmation",
            "memo", "status", "created_at", "paid_at",
        ]
        read_only_fields = fields

    def get_checkout_url(self, obj) -> str:
        return obj.amatopay_session.checkout_url if obj.amatopay_session_id else ""

    def get_awaiting_delivery_confirmation(self, obj) -> bool:
        return bool(obj.amatopay_session_id and obj.amatopay_session.awaiting_delivery_confirmation)


class POSChargeCreateSerializer(serializers.Serializer):
    amount_sats = serializers.IntegerField(min_value=1_000, help_text="Satoshis (min 1,000)")
    memo = serializers.CharField(required=False, allow_blank=True, max_length=255, default="")


class POSChargeBifCreateSerializer(serializers.Serializer):
    amount_bif = serializers.IntegerField(min_value=1, help_text="Burundian Francs, collected via mobile money.")
    payer_alias = serializers.CharField(max_length=160, help_text="Payer's mobile money alias, e.g. +25779000000")
    memo = serializers.CharField(required=False, allow_blank=True, max_length=255, default="")
    return_url = serializers.CharField(max_length=500, required=False, allow_blank=True, default="")


class AmatoPayTopupSessionSerializer(serializers.ModelSerializer):
    class Meta:
        model = AmatoPayCheckoutSession
        fields = [
            "id", "session_id", "payment_reference", "payer_alias", "payer_display_name",
            "amount_bif", "checkout_url", "status", "payment_status",
            "awaiting_delivery_confirmation", "delivery_confirmed_at",
            "created_at", "confirmed_at",
        ]
        read_only_fields = fields


class AmatoPayTopupCreateSerializer(serializers.Serializer):
    amount_bif = serializers.IntegerField(min_value=1, help_text="Burundian Francs")
    payer_alias = serializers.CharField(max_length=160, help_text="Payer's mobile money alias, e.g. +25779000000")
    return_url = serializers.CharField(max_length=500, required=False, allow_blank=True, default="")


class AmatoPayVerifyAliasSerializer(serializers.Serializer):
    payer_alias = serializers.CharField(max_length=160, help_text="Mobile money alias to verify, e.g. +25779000000")


class AmatoPayConfirmDeliverySerializer(serializers.Serializer):
    secure_code = serializers.RegexField(
        regex=r"^\d{6}$", help_text="The payer's six-digit AmatoPay release code.",
    )
