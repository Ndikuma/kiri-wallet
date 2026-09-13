from decimal import Decimal, InvalidOperation

from django.core.management.base import BaseCommand, CommandError

from wallet.models import ExchangeRate


class Command(BaseCommand):
    help = "Set the active BTC -> BIF exchange rate (BIF per whole BTC)."

    def add_arguments(self, parser):
        parser.add_argument("bif_per_btc", type=str, help="e.g. 150000000.00")
        parser.add_argument("--source", default="manual")

    def handle(self, *args, **options):
        try:
            rate_value = Decimal(options["bif_per_btc"])
        except InvalidOperation:
            raise CommandError(f"Invalid decimal: {options['bif_per_btc']!r}")

        ExchangeRate.objects.filter(is_active=True).update(is_active=False)
        rate = ExchangeRate.objects.create(bif_per_btc=rate_value, source=options["source"], is_active=True)
        self.stdout.write(self.style.SUCCESS(f"Active rate set: 1 BTC = {rate.bif_per_btc} BIF (id={rate.pk})"))
