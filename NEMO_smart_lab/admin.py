from django.contrib import admin

from NEMO_smart_lab.models import NemoApiSource, RemoteSyncEndpoint, SmartLabTool, SmartLabToolChannel


@admin.register(RemoteSyncEndpoint)
class RemoteSyncEndpointAdmin(admin.ModelAdmin):
    list_display = ("name", "username", "host", "port", "base_path")
    search_fields = ("name", "host", "username")


@admin.register(NemoApiSource)
class NemoApiSourceAdmin(admin.ModelAdmin):
    list_display = ("name", "api_root", "verify_ssl")
    search_fields = ("name", "api_root")


class SmartLabToolChannelInline(admin.TabularInline):
    model = SmartLabToolChannel
    extra = 1
    fields = ("channel_key", "display_name", "role", "on_threshold_c", "hidden")


@admin.register(SmartLabTool)
class SmartLabToolAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "local_root", "enabled", "sync_endpoint", "remote_subdir", "last_synced", "last_sync_ok")
    list_editable = ("enabled",)
    list_filter = ("kind", "enabled", "sync_endpoint")
    search_fields = ("name", "local_root")
    readonly_fields = ("last_synced", "last_sync_ok", "last_sync_message")
    inlines = [SmartLabToolChannelInline]
    fieldsets = (
        (None, {"fields": ("name", "kind", "local_root", "enabled")}),
        ("Thresholds", {"fields": ("on_threshold_c", "on_threshold_pct"), "classes": ("collapse",)}),
        (
            "Overview status (dashboard)",
            {
                "fields": ("standby_recipe_keywords", "shutdown_recipe_keywords", "valve_clean_recipe_keywords"),
                "classes": ("collapse",),
                "description": (
                    "Drives the dashboard's overview status when the tool isn't currently in use "
                    "(that always takes priority and needs no configuration): the latest completed "
                    "run's recipe name is matched, case-insensitively, against each of these in "
                    "order (shutdown, then standby, then valve clean) - first match wins, no match "
                    "falls back to a plain 'Ready'. Comma-separated. Blank disables that state for "
                    "this tool."
                ),
            },
        ),
        ("Live telemetry (cobra_job only)", {"fields": ("stream_root", "stream_module"), "classes": ("collapse",)}),
        (
            "Remote sync",
            {
                "fields": (
                    "sync_endpoint",
                    "remote_subdir",
                    "recipe_subdir",
                    "recipe_channel_offset",
                    "pinned_recipe_categories",
                    "last_synced",
                    "last_sync_ok",
                    "last_sync_message",
                )
            },
        ),
        (
            "Reservation lookup (read-only)",
            {
                "fields": ("usage_reference_source",),
                "classes": ("collapse",),
                "description": (
                    "Local Reservation/UsageEvent history (this NEMO instance's own database) is "
                    "always tried first and is the only thing ever shown as authoritative. If "
                    "set, and a run has no local match, a single read-only GET is made to the "
                    "chosen source's API (using real_id below as the Tool id there) purely for "
                    "display - nothing here can write to that instance."
                ),
            },
        ),
        ("Demo/dev seeding", {"fields": ("real_id", "real_category"), "classes": ("collapse",)}),
    )
