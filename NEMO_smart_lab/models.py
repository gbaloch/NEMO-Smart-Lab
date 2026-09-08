from django.db import models


class RemoteSyncEndpoint(models.Model):
    """
    A remote, SSH-reachable file server that tool data can be synced down from - a Stanford Oak
    data transfer node, a departmental fileserver, anything reachable the same way. Reusable
    across tools: several SmartLabTool rows can point at the same endpoint with different
    remote_subdir values.

    Deliberately generic rather than tied to any one storage provider - see
    NEMO_smart_lab/remote_sync.py for the actual sync mechanics, and for why this always
    authenticates with a registered SSH key rather than a password (Stanford's Oak is the
    motivating example: https://docs.oak.stanford.edu/gateways/, but the same "keypair against
    a DTN host, one base directory per tool underneath it" shape applies to plenty of other
    SSH-based lab storage too).
    """

    name = models.CharField(
        max_length=100,
        unique=True,
        help_text='A short label, e.g. "Oak" or "Building 34 fileserver".',
    )
    host = models.CharField(max_length=255, help_text="e.g. dtn.oak.stanford.edu")
    port = models.PositiveIntegerField(default=22)
    username = models.CharField(max_length=150, help_text="Account name on the remote host.")
    ssh_key_path = models.CharField(
        max_length=500,
        help_text=(
            "Path (on this NEMO server) to the private half of a keypair registered for "
            "public-key login on the remote host. Password/2FA login isn't supported here - an "
            "unattended sync can't answer an interactive prompt."
        ),
    )
    base_path = models.CharField(
        max_length=500,
        help_text="Remote directory that every tool's own subdirectory lives under, e.g. /oak/stanford/orgs/nano",
    )
    extra_ssh_options = models.TextField(
        blank=True,
        help_text=(
            'Optional, one "Key Value" pair per line, appended as -o Key=Value to every '
            "connection - e.g. ControlPath ~/.ssh/%r@%h:%p plus ControlPersist yes to reuse an "
            "already-authenticated ControlMaster tunnel instead of a fresh SSH login per sync."
        ),
    )

    class Meta:
        verbose_name = "Remote sync endpoint"
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.username}@{self.host})"

    def extra_ssh_option_pairs(self):
        pairs = []
        for line in self.extra_ssh_options.splitlines():
            line = line.strip()
            if not line:
                continue
            key, _, value = line.partition(" ")
            if not value:
                key, _, value = line.partition("=")
            if key and value:
                pairs.append((key.strip(), value.strip()))
        return pairs


class SmartLabTool(models.Model):
    """
    One tool's Smart Lab configuration: which raw-data reader to use, where its data lives
    locally, and (optionally) which RemoteSyncEndpoint keeps that local copy up to date.

    This is the database-backed replacement for what used to be a SMART_LAB_TOOL_SOURCES dict
    in settings.py - moved here, and managed from the Django admin (Tool Data > Smart Lab
    tools), so a lab manager can add/edit/disable a tool without a code deploy.
    """

    KIND_CHOICES = [
        ("heater_log", "Heater log (Veeco Fiji / Savannah ALD)"),
        ("mvd", "MVD run folders (Cambridge Nanotech / Veeco MVD)"),
        ("waferlog", "Wafer log (Plasma-Therm VersaLine)"),
        ("cobra_job", "Cobra job database (Oxford PlasmaPro 100 Cobra / PTIQ)"),
        ("eventlog", "Event log (KLA-DSE / Trikon-SPTS fxPLPXTMC)"),
    ]

    name = models.CharField(max_length=200, unique=True, help_text="Must exactly match a real NEMO Tool.name.")
    kind = models.CharField(max_length=20, choices=KIND_CHOICES)
    local_root = models.CharField(
        max_length=500,
        help_text=(
            "Local path this tool's raw data is read from - a network share, or a directory "
            "kept mirrored by sync_remote_data from the Remote sync endpoint below."
        ),
    )
    enabled = models.BooleanField(default=True, help_text="Uncheck to hide this tool from the Smart Lab dashboard.")

    on_threshold_c = models.FloatField(
        null=True,
        blank=True,
        help_text='heater_log/mvd only - a heater channel above this many °C counts as "on". Blank uses the reader default (35.0).',
    )
    on_threshold_pct = models.FloatField(
        null=True,
        blank=True,
        help_text='mvd only - a heater channel with duty cycle above this percent counts as "on". Blank uses the reader default (0.5).',
    )

    stream_root = models.CharField(
        max_length=500,
        blank=True,
        help_text=(
            "cobra_job only, optional - local path to a synced copy of "
            "PTIQ/Databases/StreamedData, for the tool detail page's live telemetry section."
        ),
    )
    stream_module = models.CharField(
        max_length=100,
        blank=True,
        default="PMC1",
        help_text="cobra_job only - the StreamedData module subfolder name.",
    )

    sync_endpoint = models.ForeignKey(
        RemoteSyncEndpoint,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        help_text="Optional - if set, `sync_remote_data` mirrors this tool's data down from this endpoint into local_root.",
    )
    remote_subdir = models.CharField(
        max_length=200,
        blank=True,
        help_text=(
            "Directory name under the endpoint's base_path (e.g. the <toolname> in "
            "/oak/stanford/orgs/nano/<toolname>). Leave blank to use the tool name above as-is."
        ),
    )
    last_synced = models.DateTimeField(null=True, blank=True, editable=False)
    last_sync_ok = models.BooleanField(null=True, blank=True, editable=False)
    last_sync_message = models.CharField(max_length=500, blank=True, editable=False)

    real_id = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Optional - used only by seed_smart_lab_demo, to create/align a dev database's Tool at this exact primary key.",
    )
    real_category = models.CharField(
        max_length=200,
        blank=True,
        help_text="Optional - used only by seed_smart_lab_demo, as the demo Tool's category.",
    )

    class Meta:
        verbose_name = "Smart Lab tool"
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def remote_subdir_or_default(self):
        return self.remote_subdir.strip() or self.name

    def as_source_config(self):
        """Builds the {"kind": ..., "root": ..., ...} dict shape NEMO_smart_lab.readers expects."""
        cfg = {"kind": self.kind, "root": self.local_root}
        if self.on_threshold_c is not None:
            cfg["on_threshold_c"] = self.on_threshold_c
        if self.on_threshold_pct is not None:
            cfg["on_threshold_pct"] = self.on_threshold_pct
        if self.stream_root:
            cfg["stream_root"] = self.stream_root
            cfg["stream_module"] = self.stream_module or "PMC1"
        return cfg
