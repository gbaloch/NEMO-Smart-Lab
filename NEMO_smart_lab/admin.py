from django.contrib import admin

from NEMO_smart_lab.models import RemoteSyncEndpoint, SmartLabTool


@admin.register(RemoteSyncEndpoint)
class RemoteSyncEndpointAdmin(admin.ModelAdmin):
    list_display = ("name", "username", "host", "port", "base_path")
    search_fields = ("name", "host", "username")


@admin.register(SmartLabTool)
class SmartLabToolAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "local_root", "enabled", "sync_endpoint", "remote_subdir", "last_synced", "last_sync_ok")
    list_editable = ("enabled",)
    list_filter = ("kind", "enabled", "sync_endpoint")
    search_fields = ("name", "local_root")
    readonly_fields = ("last_synced", "last_sync_ok", "last_sync_message")
    fieldsets = (
        (None, {"fields": ("name", "kind", "local_root", "enabled")}),
        ("Thresholds", {"fields": ("on_threshold_c", "on_threshold_pct"), "classes": ("collapse",)}),
        ("Live telemetry (cobra_job only)", {"fields": ("stream_root", "stream_module"), "classes": ("collapse",)}),
        ("Remote sync", {"fields": ("sync_endpoint", "remote_subdir", "last_synced", "last_sync_ok", "last_sync_message")}),
        ("Demo/dev seeding", {"fields": ("real_id", "real_category"), "classes": ("collapse",)}),
    )
