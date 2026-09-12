from django.db import migrations

OLD_DEFAULT = "valve clean, clean valve"
NEW_DEFAULT = "valve clean, clean valve, purge, clean, ozone clean, clear, clear0"


def backfill_forwards(apps, schema_editor):
    # Only rows still holding the exact original seeded default are touched - an admin who has
    # already customized this field for a specific tool keeps their own value untouched.
    SmartLabTool = apps.get_model("smart_lab", "SmartLabTool")
    SmartLabTool.objects.filter(valve_clean_recipe_keywords=OLD_DEFAULT).update(valve_clean_recipe_keywords=NEW_DEFAULT)


def backfill_backwards(apps, schema_editor):
    SmartLabTool = apps.get_model("smart_lab", "SmartLabTool")
    SmartLabTool.objects.filter(valve_clean_recipe_keywords=NEW_DEFAULT).update(valve_clean_recipe_keywords=OLD_DEFAULT)


class Migration(migrations.Migration):

    dependencies = [
        ("smart_lab", "0011_alter_smartlabtool_valve_clean_recipe_keywords"),
    ]

    operations = [
        migrations.RunPython(backfill_forwards, backfill_backwards),
    ]
