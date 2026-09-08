from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from NEMO_smart_lab.models import SmartLabTool
from NEMO_smart_lab.remote_sync import RemoteSyncError, sync_tool_from_remote


class Command(BaseCommand):
    help = (
        "Pulls each SmartLabTool's raw data down from its configured Remote sync endpoint (if "
        "any) into its local_root, over SSH with a registered public key. Configure tools and "
        "endpoints in the Django admin under Tool Data - see NEMO_smart_lab/remote_sync.py for "
        "how the transfer itself works."
    )

    def add_arguments(self, parser):
        parser.add_argument("tool", nargs="?", help="Only sync this one tool (by SmartLabTool.name).")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be transferred without copying anything (rsync only - no-op with the scp fallback).",
        )

    def handle(self, tool=None, dry_run=False, **options):
        queryset = SmartLabTool.objects.exclude(sync_endpoint=None)
        if tool:
            queryset = queryset.filter(name=tool)
            if not queryset.exists():
                raise CommandError(f"No SmartLabTool named '{tool}' with a Remote sync endpoint configured.")

        if not queryset.exists():
            self.stdout.write("No tools have a Remote sync endpoint configured - nothing to do.")
            return

        failures = []
        for smart_tool in queryset:
            try:
                message = sync_tool_from_remote(
                    smart_tool.local_root, smart_tool.sync_endpoint, smart_tool.remote_subdir_or_default, dry_run=dry_run
                )
            except RemoteSyncError as e:
                failures.append(smart_tool.name)
                self.stderr.write(self.style.ERROR(f"{smart_tool.name}: {e}"))
                if not dry_run:
                    smart_tool.last_synced = timezone.now()
                    smart_tool.last_sync_ok = False
                    smart_tool.last_sync_message = str(e)[:500]
                    smart_tool.save(update_fields=["last_synced", "last_sync_ok", "last_sync_message"])
                continue

            self.stdout.write(self.style.SUCCESS(f"{smart_tool.name}: {message}"))
            if not dry_run:
                smart_tool.last_synced = timezone.now()
                smart_tool.last_sync_ok = True
                smart_tool.last_sync_message = message[:500]
                smart_tool.save(update_fields=["last_synced", "last_sync_ok", "last_sync_message"])

        if failures:
            raise CommandError(f"Sync failed for: {', '.join(failures)}")
