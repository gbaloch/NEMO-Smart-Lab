from django.apps import AppConfig


class SmartLabConfig(AppConfig):
    name = "NEMO_smart_lab"
    verbose_name = "Smart Lab"

    def ready(self):
        """
        This code will be run when Django starts.
        """
        from NEMO.plugins.utils import check_extra_dependencies

        check_extra_dependencies(self.name, ["NEMO", "NEMO-CE"])
