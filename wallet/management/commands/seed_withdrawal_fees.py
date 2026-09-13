from django.core.management.base import BaseCommand

from wallet.models import DEFAULT_WITHDRAWAL_FEE_POLICIES, WithdrawalFeePolicy


class Command(BaseCommand):
    help = "Create or update default wallet withdrawal fee policies."

    def handle(self, *args, **options):
        for target_type, defaults in DEFAULT_WITHDRAWAL_FEE_POLICIES.items():
            policy, created = WithdrawalFeePolicy.objects.update_or_create(
                target_type=target_type,
                defaults={
                    "display_label": defaults.get("display_label", target_type.replace("_", " ").title()),
                    "fixed_fee_sats": defaults.get("fixed_fee_sats", 0),
                    "percent_fee_bps": defaults.get("percent_fee_bps", 0),
                    "min_fee_sats": defaults.get("min_fee_sats", 0),
                    "max_fee_sats": defaults.get("max_fee_sats", 0),
                    "charge_to_user": defaults.get("charge_to_user", False),
                    "description": defaults.get("description", ""),
                    "is_active": True,
                },
            )
            action = "Created" if created else "Updated"
            self.stdout.write(self.style.SUCCESS(
                f"{action}: {policy.display_label} | fixed={policy.fixed_fee_sats} sats | "
                f"bps={policy.percent_fee_bps} | charge_to_user={policy.charge_to_user}"
            ))
