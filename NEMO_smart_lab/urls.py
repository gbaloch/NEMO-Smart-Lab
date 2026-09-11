from django.urls import path

from NEMO_smart_lab import views

urlpatterns = [
    path("smart_lab/", views.dashboard, name="smart_lab_dashboard"),
    path("smart_lab/tool/<slug:tool_slug>/", views.tool_detail, name="smart_lab_tool_detail"),
    path("smart_lab/tool/<slug:tool_slug>/history/", views.tool_history, name="smart_lab_tool_history"),
    path("smart_lab/tool/<slug:tool_slug>/chart.png", views.tool_chart, name="smart_lab_tool_chart"),
    path("smart_lab/tool/<slug:tool_slug>/stream.png", views.tool_stream_chart, name="smart_lab_tool_stream_chart"),
    path("smart_lab/tool/<slug:tool_slug>/screenshot.jpg", views.tool_screenshot, name="smart_lab_tool_screenshot"),
    path("smart_lab/tool/<slug:tool_slug>/chart.json", views.tool_chart_data, name="smart_lab_tool_chart_data"),
    path(
        "smart_lab/tool/<slug:tool_slug>/stream.json",
        views.tool_stream_chart_data,
        name="smart_lab_tool_stream_chart_data",
    ),
]
