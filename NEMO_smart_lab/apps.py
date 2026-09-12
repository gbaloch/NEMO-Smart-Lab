from django.apps import AppConfig


class SmartLabConfig(AppConfig):
    name = "NEMO_smart_lab"
    label = "smart_lab"
    verbose_name = "NEMO Smart Lab"
    default_auto_field = "django.db.models.AutoField"

    def ready(self):
        from NEMO.plugins.utils import check_extra_dependencies

        """
        This code will be run when Django starts.
        """
        check_extra_dependencies(self.name, ["NEMO", "NEMO-CE"])
