from django.urls import path

from NEMO_smart_lab import views

urlpatterns = [
    path("smart_lab/", views.dashboard, name="smart_lab_dashboard"),
    path("smart_lab/api/sync-map.json", views.tool_sync_map, name="smart_lab_tool_sync_map"),
    path("smart_lab/tool/<int:tool_id>/", views.tool_detail, name="smart_lab_tool_detail"),
    path("smart_lab/tool/<int:tool_id>/history/", views.tool_history, name="smart_lab_tool_history"),
    path("smart_lab/tool/<int:tool_id>/chart.png", views.tool_chart, name="smart_lab_tool_chart"),
    path("smart_lab/tool/<int:tool_id>/stream.png", views.tool_stream_chart, name="smart_lab_tool_stream_chart"),
    path("smart_lab/tool/<int:tool_id>/screenshot.jpg", views.tool_screenshot, name="smart_lab_tool_screenshot"),
    path("smart_lab/tool/<int:tool_id>/chart.json", views.tool_chart_data, name="smart_lab_tool_chart_data"),
    path(
        "smart_lab/tool/<int:tool_id>/stream.json",
        views.tool_stream_chart_data,
        name="smart_lab_tool_stream_chart_data",
    ),
    path(
        "smart_lab/tool/<int:tool_id>/base_pressure.json",
        views.tool_base_pressure_data,
        name="smart_lab_tool_base_pressure_data",
    ),
    path(
        "smart_lab/tool/<int:tool_id>/base_pressure.png",
        views.tool_base_pressure_chart,
        name="smart_lab_tool_base_pressure_chart",
    ),
    path(
        "smart_lab/tool/<int:tool_id>/base_pressure.csv",
        views.tool_base_pressure_csv,
        name="smart_lab_tool_base_pressure_csv",
    ),
    path(
        "smart_lab/tool/<int:tool_id>/continuous_pressure.json",
        views.tool_continuous_pressure_data,
        name="smart_lab_tool_continuous_pressure_data",
    ),
    path("smart_lab/tool/<int:tool_id>/data/", views.tool_data, name="smart_lab_tool_data"),
    path("smart_lab/tool/<int:tool_id>/recipes/", views.tool_recipes, name="smart_lab_tool_recipes"),
    path(
        "smart_lab/tool/<int:tool_id>/recipes/toggle-pin/",
        views.tool_recipe_toggle_pin,
        name="smart_lab_tool_recipe_toggle_pin",
    ),
    path(
        "smart_lab/tool/<int:tool_id>/recipes/duplicates/",
        views.tool_recipe_duplicates,
        name="smart_lab_tool_recipe_duplicates",
    ),
    path(
        "smart_lab/tool/<int:tool_id>/recipes/<str:recipe_id>/",
        views.tool_recipe_detail,
        name="smart_lab_tool_recipe_detail",
    ),
    path(
        "smart_lab/tool/<int:tool_id>/maintenance/",
        views.tool_maintenance_trends,
        name="smart_lab_tool_maintenance_trends",
    ),
    path("smart_lab/tool/<int:tool_id>/configs/", views.tool_configs, name="smart_lab_tool_configs"),
    path(
        "smart_lab/tool/<int:tool_id>/configs/<str:file_id>/",
        views.tool_config_detail,
        name="smart_lab_tool_config_detail",
    ),
]
