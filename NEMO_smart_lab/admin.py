from django.contrib import admin, messages

from NEMO_smart_lab.models import NemoApiSource, RemoteSyncEndpoint, SmartLabTool, SmartLabToolChannel
from NEMO_smart_lab.recipes import suggest_base_pressure_recipes


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
    actions = ["suggest_base_pressure_recipes_action"]

    @admin.action(description="Auto-detect standby recipes for base pressure tracking")
    def suggest_base_pressure_recipes_action(self, request, queryset):
        for tool in queryset:
            if not tool.recipe_subdir:
                self.message_user(request, f"{tool.name}: no recipe_subdir configured - nothing to scan.", level=messages.WARNING)
                continue
            found = suggest_base_pressure_recipes(
                tool.as_source_config(), tool.standby_recipe_keywords, tool.valve_clean_recipe_keywords
            )
            if not found:
                self.message_user(
                    request,
                    f"{tool.name}: no candidates found (checked recipes matching keyword(s) "
                    f"'{tool.standby_recipe_keywords}' whose last step is a wait).",
                    level=messages.WARNING,
                )
                continue
            tool.base_pressure_recipe_names = ", ".join(found)
            tool.save(update_fields=["base_pressure_recipe_names"])
            self.message_user(request, f"{tool.name}: set base_pressure_recipe_names to {len(found)} candidate(s): {', '.join(found)}")

    fieldsets = (
        (None, {"fields": ("name", "kind", "local_root", "enabled")}),
        ("Thresholds", {"fields": ("on_threshold_c", "on_threshold_pct"), "classes": ("collapse",)}),
        (
            "Overview status (dashboard)",
            {
                "fields": ("standby_recipe_keywords", "shutdown_recipe_keywords", "valve_clean_recipe_keywords"),
                "classes": ("collapse",),
                "description": "Sets the dashboard's status badge when the tool isn't currently in use.",
            },
        ),
        (
            "Chamber base pressure history (tool detail page)",
            {
                "fields": ("base_pressure_recipe_names", "continuous_pressure_subdir"),
                "classes": ("collapse",),
                "description": "Plots average chamber pressure over time from matching standby runs, "
                "and optionally a continuous always-on pressure log if this tool has one.",
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
                    "config_subdir",
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
                "description": "Local usage history is always tried first; this is a read-only fallback.",
            },
        ),
        ("Demo/dev seeding", {"fields": ("real_id", "real_category"), "classes": ("collapse",)}),
    )
