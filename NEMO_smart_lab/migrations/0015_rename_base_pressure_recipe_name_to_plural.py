from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("smart_lab", "0014_smartlabtool_base_pressure_recipe_name"),
    ]

    operations = [
        # A rename (not drop+add) - preserves each tool's already-configured single recipe name
        # as the first (only) entry of its new comma-separated list.
        migrations.RenameField(
            model_name="smartlabtool",
            old_name="base_pressure_recipe_name",
            new_name="base_pressure_recipe_names",
        ),
        migrations.AlterField(
            model_name="smartlabtool",
            name="base_pressure_recipe_names",
            field=models.CharField(
                blank=True,
                max_length=500,
                help_text=(
                    "Comma-separated, exact recipe names (as embedded in the run filename/folder "
                    "name, e.g. '20 - STANDBY 200C, 00 - STANDBY') to track for the chamber "
                    "base-pressure history chart on the tool detail page - the average of each "
                    "matching run's last 10 seconds of pressure data, plotted over time. Exact "
                    "names, not keywords/substrings: a tool can have several different standby-ish "
                    "recipes that all end in the same long wait step (confirmed real - see the "
                    "'Auto-detect standby recipes' admin action below), but a truly unrelated "
                    "recipe that merely contains 'standby' in its name could have a very different "
                    "baseline pressure and would skew the trend. Blank hides this chart for this "
                    "tool."
                ),
            ),
        ),
    ]
