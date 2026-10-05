"""
Management command: check_providers — health-check the block explorer providers.

    python manage.py check_providers              # providers for BITCOIN_NETWORK
    python manage.py check_providers --all        # every network
    python manage.py check_providers --inactive   # include disabled providers

Exits with status 1 if no active provider for the configured network is healthy,
so it can be used from cron/monitoring.
"""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from wallet.esplora_client import check_providers
from wallet.models import BlockExplorerProvider


class Command(BaseCommand):
    help = "Check that block explorer providers are reachable, on the right chain, up to date and returning fees."

    def add_arguments(self, parser):
        parser.add_argument("--all", action="store_true", help="Check providers of every network.")
        parser.add_argument("--inactive", action="store_true", help="Also check disabled providers.")

    def handle(self, *args, **options):
        rows = BlockExplorerProvider.objects.all()
        if not options["all"]:
            rows = rows.filter(network=settings.BITCOIN_NETWORK)
        if not options["inactive"]:
            rows = rows.filter(is_active=True)
        rows = list(rows.order_by("network", "priority"))
        if not rows:
            raise CommandError(f"No providers to check for {settings.BITCOIN_NETWORK}. Add one in the admin.")

        healthy_current = 0
        for result in check_providers(rows):
            provider = result["provider"]
            line = f"[{provider.network}] {provider.name} ({provider.api_url}): {result['message']}"
            if result["ok"]:
                self.stdout.write(self.style.SUCCESS(f"OK    {line}"))
                if provider.network == settings.BITCOIN_NETWORK and provider.is_active:
                    healthy_current += 1
            else:
                self.stdout.write(self.style.ERROR(f"FAIL  {line}"))

        if not healthy_current:
            raise CommandError(f"No healthy active provider for {settings.BITCOIN_NETWORK}: on-chain features will fail.")
