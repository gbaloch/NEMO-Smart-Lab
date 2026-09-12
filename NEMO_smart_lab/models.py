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


class NemoApiSource(models.Model):
    """
    A *different* NEMO instance's REST API (e.g. production), used strictly as an optional,
    read-only reference source for NEMO_smart_lab.reservations.get_run_usage() when this local
    instance has no matching Reservation/UsageEvent of its own yet - see that module for the
    actual lookup. Nothing in this codebase ever issues anything but a GET against api_root; there
    is no code path here capable of writing to whatever instance this points at.

    Not configured by default - a SmartLabTool only consults one of these if an admin explicitly
    sets its usage_reference_source, so no local install ever talks to a remote NEMO unless
    someone deliberately opts a tool into it.
    """

    name = models.CharField(max_length=100, unique=True, help_text='A short label, e.g. "Stanford prod".')
    api_root = models.URLField(help_text="Base REST API URL, e.g. https://nemo.stanford.edu/api")
    token = models.CharField(max_length=200, help_text="Value sent as 'Authorization: Token <this>' - read-only access is enough.")
    verify_ssl = models.BooleanField(default=True)

    class Meta:
        verbose_name = "NEMO API source (read-only)"
        ordering = ["name"]

    def __str__(self):
        return self.name


class SmartLabTool(models.Model):
    """
    One tool's Smart Lab configuration: which raw-data reader to use, where its data lives
    locally, and (optionally) which RemoteSyncEndpoint keeps that local copy up to date.

    This is the database-backed replacement for what used to be a SMART_LAB_TOOL_SOURCES dict
    in settings.py - moved here, and managed from the Django admin (Smart Lab > Smart Lab
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

    recipe_subdir = models.CharField(
        max_length=200,
        blank=True,
        help_text=(
            "Optional, read-only - directory name under remote_subdir holding this tool's recipe "
            'files (e.g. "Recipes" or "recipes" - varies per tool, check the exact case on the '
            "remote host). Leave blank to hide the Recipes tab for this tool entirely."
        ),
    )
    recipe_channel_offset = models.IntegerField(
        default=0,
        help_text=(
            "heater_log only - a recipe file's \"heater\" step lines reference this tool's own "
            "physical channel numbering, which for some tools doesn't match the heater log file's "
            'own column numbering (channel_labels\' "Heater N" keys) by a fixed amount - e.g. '
            "confirmed for fiji1/2/3: recipe channel 12 (\"Cone\") is the log's \"Heater 6\", a "
            "constant +6 offset (recipe_channel - offset = the log's number). Confirmed 0 (no "
            "translation needed) for savannah, where recipe channel numbers already match the log's "
            "own numbering directly. Not derivable automatically - verify against a real recipe file "
            "and this tool's existing channel_labels before setting. Irrelevant for mvd-kind tools "
            "(their recipe channel numbers already match channel_labels' bare-number keys as-is)."
        ),
    )
    pinned_recipe_categories = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            "Recipe folder names (exact match, e.g. 'STANDARD' or 'Special Project') pinned to "
            "always sort first on this tool's Recipes page, ahead of even '(root)'. Normally "
            "toggled from the small pin icon next to each folder heading there rather than edited "
            "here directly."
        ),
    )

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

    usage_reference_source = models.ForeignKey(
        NemoApiSource,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        help_text=(
            "Optional, read-only - if this tool's own local Reservation/UsageEvent history has "
            "nothing for a given run, NEMO_smart_lab.reservations.get_run_usage() will look the "
            "run up on this remote NEMO instance's API instead (GET only, never written back), "
            "using real_id above as that instance's Tool id. Leave blank to only ever use local data."
        ),
    )

    base_pressure_recipe_names = models.CharField(
        max_length=500,
        blank=True,
        help_text=(
            "Comma-separated, exact recipe names (as embedded in the run filename/folder name, "
            "e.g. '20 - STANDBY 200C, 00 - STANDBY') to track for the chamber base-pressure "
            "history chart on the tool detail page - the average of each matching run's last 10 "
            "seconds of pressure data, plotted over time. Exact names, not keywords/substrings: a "
            "tool can have several different standby-ish recipes that all end in the same long "
            "wait step (confirmed real - see the 'Auto-detect standby recipes' admin action below), "
            "but a truly unrelated recipe that merely contains 'standby' in its name could have a "
            "very different baseline pressure and would skew the trend. Blank hides this chart for "
            "this tool."
        ),
    )

    # NEMO_smart_lab.status's overview-page "meaningful status" - a currently open usage event
    # always wins (checked live, not from these), otherwise the latest completed run's recipe name
    # is matched (case-insensitive substring) against each of these, in this order: shutdown,
    # standby, valve clean - first match wins, no match falls back to a plain "Ready". Pre-filled
    # with sensible defaults so this "just works" out of the box, but each is per-tool editable
    # since real recipe-naming conventions vary tool to tool; blank disables that state entirely
    # for this tool (it's simply never matched).
    standby_recipe_keywords = models.CharField(
        max_length=300,
        blank=True,
        default="standby",
        help_text=(
            'Comma-separated, case-insensitive substrings - when the latest run\'s recipe name '
            "contains one of these and the tool isn't currently in use, the dashboard shows "
            "'Ready - standby' instead of a plain 'Ready'. Blank disables this state for this tool."
        ),
    )
    shutdown_recipe_keywords = models.CharField(
        max_length=300,
        blank=True,
        default="shutdown, shut down",
        help_text=(
            'Comma-separated, case-insensitive substrings - when the latest run\'s recipe name '
            "contains one of these and the tool isn't currently in use, the dashboard shows "
            "'Shut down' instead of 'Ready'. Checked before standby/valve-clean. Blank disables "
            "this state for this tool."
        ),
    )
    valve_clean_recipe_keywords = models.CharField(
        max_length=300,
        blank=True,
        default="valve clean, clean valve, purge, clean, ozone clean, clear, clear0",
        help_text=(
            'Comma-separated, case-insensitive substrings - when the latest run\'s recipe name '
            "contains one of these and the tool isn't currently in use, the dashboard shows "
            "'Ready - clean' instead of a plain 'Ready'. Checked after shutdown/standby. "
            "Blank disables this state for this tool."
        ),
    )

    class Meta:
        verbose_name = "Smart Lab tool"
        ordering = ["name"]
        permissions = [
            (
                "access_smart_lab",
                "Can access Smart Lab (beyond staff/superusers, who can always access it)",
            )
        ]

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
        channel_labels = {
            c.channel_key: (c.display_name, c.role, c.hidden, c.on_threshold_c) for c in self.channel_labels.all()
        }
        if channel_labels:
            cfg["channel_labels"] = channel_labels
        if self.recipe_subdir:
            cfg["recipe_subdir"] = self.recipe_subdir
        if self.recipe_channel_offset:
            cfg["recipe_channel_offset"] = self.recipe_channel_offset
        if self.pinned_recipe_categories:
            cfg["pinned_recipe_categories"] = self.pinned_recipe_categories
        if self.base_pressure_recipe_names:
            cfg["base_pressure_recipe_names"] = self.base_pressure_recipe_names
        if self.sync_endpoint_id:
            # Presence of this key is what tells readers.py to fetch lazily via remote_cache
            # instead of assuming local_root is a fully pre-populated mirror - see
            # NEMO_smart_lab.remote_cache's module docstring.
            cfg["remote_tool"] = self
        return cfg


class SmartLabToolChannel(models.Model):
    """
    A human-readable override for one raw channel key on a SmartLabTool - e.g. mapping the raw
    "Heater 3" (heater_log) or "6" (mvd HTR6) key readers.py works with internally to a physical
    description like "Source chuck". There's no way to auto-derive this: the two real data
    sources that *could* carry it (Fiji1/2's Setup.ini.txt "Heater<n>" name field, and MVD/Fiji5's
    _SUM.txt "HTR<n>=\"label\"" field, already parsed by readers._HEATER_LABEL_RE) both exist in
    the file formats but are blank in every real Stanford SNF export - so this is deliberately a
    manually-curated, per-tool admin setting instead of anything automatic.
    """

    ROLE_CHOICES = [
        ("chuck", "Chuck"),
        ("chamber", "Chamber wall"),
        ("reactor", "Reactor"),
        ("source_valve", "Source valve"),
        ("precursor_line", "Jacket"),
        ("delivery_line", "Delivery line"),
        ("exhaust", "Exhaust"),
        ("other", "Other"),
    ]

    tool = models.ForeignKey(SmartLabTool, on_delete=models.CASCADE, related_name="channel_labels")
    channel_key = models.CharField(
        max_length=100,
        help_text='The raw channel name/number as shown today in the tool\'s channel table, e.g. "Heater 3" or "6".',
    )
    display_name = models.CharField(
        max_length=200, blank=True, help_text='Friendly name to show instead, e.g. "Source chuck". Ignored if hidden.'
    )
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default="other", blank=True)
    on_threshold_c = models.FloatField(
        null=True,
        blank=True,
        help_text=(
            "heater_log only - overrides the tool-wide on_threshold_c (SmartLabTool field) for "
            "just this one channel, e.g. a precursor jacket kept at a lower steady-state "
            "temperature than the reactor/chuck zones the tool-wide threshold is tuned for. "
            "Blank uses the tool-wide threshold."
        ),
    )
    hidden = models.BooleanField(
        default=False,
        help_text=(
            "Hide this channel entirely from the tool's detail page and chart - for a schema slot "
            "that exists in the raw log format but isn't actually wired to anything on this "
            "particular tool (reads a constant 0 forever). display_name/role are ignored when set."
        ),
    )

    class Meta:
        verbose_name = "Channel label"
        unique_together = ("tool", "channel_key")
        ordering = ["tool", "channel_key"]

    def __str__(self):
        return f"{self.tool.name}: {self.channel_key} -> {self.display_name}"
