import shutil
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from NEMO_smart_lab.models import SmartLabTool


class Command(BaseCommand):
    help = (
        "Reconciles the local demo database with the SmartLabTool table: creates/renames a "
        "Tool record for each configured Smart Lab tool (using its real production Tool ID "
        "where known - see the SmartLabTool.real_id field), removes every other (splash-pad "
        "demo) Tool and whatever cascades from it (reservations, usage events, tasks, "
        "comments, etc. tied only to those dummy tools), and makes sure the 'Smart Lab' "
        "landing page tile exists. Safe to run multiple times."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--keep-other-tools",
            action="store_true",
            help="Don't delete Tool records that aren't configured as a SmartLabTool (default: delete them).",
        )

    def handle(self, *args, **options):
        from NEMO.models import LandingPageChoice, Tool

        with transaction.atomic():
            self._reconcile_tools(Tool)
            if not options["keep_other_tools"]:
                self._prune_other_tools(Tool)

        self._seed_landing_tile(LandingPageChoice)

    def _reconcile_tools(self, Tool):
        for smart_tool in SmartLabTool.objects.all():
            name = smart_tool.name
            real_id = smart_tool.real_id
            category = smart_tool.real_category

            if real_id is not None:
                # Free up the real_id if some other (demo/dummy) tool currently holds it.
                holder = Tool.objects.filter(id=real_id).exclude(name=name).first()
                if holder is not None:
                    self.stdout.write(f"Freeing Tool id {real_id} (was '{holder.name}') for '{name}'")
                    holder.delete()

                # If this tool already exists under some other id (e.g. from before real IDs
                # were known), drop that row so it can be recreated at its real_id below.
                Tool.objects.filter(name=name).exclude(id=real_id).delete()

                tool, created = Tool.objects.update_or_create(
                    id=real_id,
                    defaults={"name": name, "_category": category, "visible": True, "_operational": True},
                )
            else:
                tool, created = Tool.objects.update_or_create(
                    name=name, defaults={"_category": category, "visible": True, "_operational": True}
                )

            action = "Created" if created else "Verified"
            self.stdout.write(self.style.SUCCESS(f"{action} Tool: {name} (id {tool.id})"))

    def _prune_other_tools(self, Tool):
        keep_names = set(SmartLabTool.objects.values_list("name", flat=True))
        stale = Tool.objects.exclude(name__in=keep_names)
        count = stale.count()
        if count:
            stale.delete()
            self.stdout.write(
                self.style.SUCCESS(f"Removed {count} non-Smart-Lab demo tool(s) and their related records")
            )
        else:
            self.stdout.write("No non-Smart-Lab demo tools to remove")

    def _seed_landing_tile(self, LandingPageChoice):
        icon_name = "smart_lab.png"
        media_root = Path(settings.MEDIA_ROOT)
        media_root.mkdir(parents=True, exist_ok=True)
        dest_icon = media_root / icon_name

        # Shipped inside the plugin package itself (NEMO_smart_lab/static/NEMO_smart_lab/lab.png)
        # rather than assumed to exist somewhere under the *NEMO* checkout - this way it's always
        # there regardless of how/where NEMO-Smart-Lab is installed (editable checkout or a real
        # `pip install`). Always re-copied (not just "if missing") so replacing lab.png in the
        # plugin and re-running this command keeps the landing page tile in sync.
        source_icon = Path(__file__).resolve().parent.parent.parent / "static" / "NEMO_smart_lab" / "lab.png"
        if source_icon.exists():
            shutil.copy(source_icon, dest_icon)
            self.stdout.write(self.style.SUCCESS(f"Copied icon to {dest_icon}"))
        else:
            self.stdout.write(self.style.WARNING(f"Icon source not found: {source_icon}, skipping icon copy"))

        choice, created = LandingPageChoice.objects.get_or_create(
            name="Smart Lab",
            defaults={
                "image": icon_name,
                "url": "/smart_lab/",
                "display_order": 100,
                "open_in_new_tab": False,
                "secure_referral": False,
                "view_permissions": "is_authenticated",
            },
        )
        if created:
            self.stdout.write(self.style.SUCCESS("Created landing page tile: Smart Lab"))
        else:
            if choice.image != icon_name:
                choice.image = icon_name
                choice.save(update_fields=["image"])
            self.stdout.write("Landing page tile already exists: Smart Lab")
