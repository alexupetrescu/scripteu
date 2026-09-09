from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models.functions import Concat
from django.utils import timezone


class GlobalSettings(models.Model):
    """Singleton row holding the emergency stop and shared defaults."""

    paused = models.BooleanField(
        default=False,
        help_text='Master kill switch. While on, no run may contact anyone.',
    )
    hard_daily_ceiling = models.PositiveIntegerField(
        default=settings.ESC_HARD_DAILY_CEILING,
        help_text='Absolute cap across all profiles, whatever a profile asks for.',
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = verbose_name_plural = 'Global settings'

    def __str__(self):
        return 'Global settings'

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):  # pragma: no cover - guard only
        raise RuntimeError('The global settings row cannot be deleted.')

    @classmethod
    def load(cls) -> 'GlobalSettings':
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class MessageTemplate(models.Model):
    name = models.CharField(max_length=120, unique=True)
    subject = models.CharField(max_length=255, blank=True)
    body = models.TextField(
        help_text='Placeholders: {name}, {first_name}, {id}',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class SearchProfile(models.Model):
    """A saved set of search filters plus the pacing used when acting on them."""

    name = models.CharField(max_length=120, unique=True)
    pass_id = models.CharField(
        max_length=32,
        help_text='Placement id from the portal URL, e.g. 82413.',
    )
    filters = models.JSONField(
        default=dict, blank=True,
        help_text='Search filters as {field: value}, matching the portal form.',
    )
    template = models.ForeignKey(
        MessageTemplate, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='profiles',
    )

    per_run_cap = models.PositiveIntegerField(
        default=settings.ESC_DEFAULT_PER_RUN_CAP,
        validators=[MinValueValidator(1)],
    )
    per_day_cap = models.PositiveIntegerField(
        default=settings.ESC_DEFAULT_PER_DAY_CAP,
        validators=[MinValueValidator(1)],
    )
    min_delay_s = models.PositiveIntegerField(default=settings.ESC_DEFAULT_MIN_DELAY)
    max_delay_s = models.PositiveIntegerField(default=settings.ESC_DEFAULT_MAX_DELAY)
    max_pages = models.PositiveIntegerField(
        default=20,
        help_text='Stop paginating after this many result pages.',
    )

    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name

    def effective_daily_cap(self) -> int:
        return min(self.per_day_cap, GlobalSettings.load().hard_daily_ceiling)

    def sent_today(self) -> int:
        """Messages that went out today — counting unconfirmed claims.

        A SENDING row may or may not have reached the portal, so it spends the
        cap. Erring the other way would let a crash loop re-spend it.
        """
        start = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
        return Outreach.objects.filter(
            search_profile=self,
            status__in=Outreach.BLOCKING,
            sent_at__gte=start,
        ).count()

    def remaining_today(self) -> int:
        return max(0, self.effective_daily_cap() - self.sent_today())


class Candidate(models.Model):
    """A person in the ESC pool, stored as thinly as possible.

    Only the portal's opaque id is required. ``display_name`` exists purely so
    the run log is readable by a human; nothing else about the person is
    copied off the portal.
    """

    external_id = models.CharField(max_length=64, unique=True, db_index=True)
    display_name = models.CharField(max_length=255, blank=True)
    profile_url = models.URLField(blank=True, max_length=500)
    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-last_seen_at']

    def __str__(self):
        return self.display_name or f'Candidate {self.external_id}'


class Run(models.Model):
    class State(models.TextChoices):
        QUEUED = 'QUEUED', 'Queued'
        RUNNING = 'RUNNING', 'Running'
        DONE = 'DONE', 'Done'
        FAILED = 'FAILED', 'Failed'
        STOPPED = 'STOPPED', 'Stopped'
        NEEDS_LOGIN = 'NEEDS_LOGIN', 'Needs login'

    search_profile = models.ForeignKey(
        SearchProfile, on_delete=models.CASCADE, related_name='runs'
    )
    state = models.CharField(max_length=16, choices=State.choices, default=State.QUEUED)
    dry_run = models.BooleanField(default=True)
    stop_requested = models.BooleanField(default=False)

    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    found_count = models.PositiveIntegerField(default=0)
    contacted_count = models.PositiveIntegerField(default=0)
    skipped_count = models.PositiveIntegerField(default=0)
    failed_count = models.PositiveIntegerField(default=0)

    log = models.TextField(blank=True)
    error = models.TextField(blank=True)

    class Meta:
        ordering = ['-id']

    def __str__(self):
        kind = 'dry run' if self.dry_run else 'live run'
        return f'{self.search_profile.name} — {kind} #{self.pk}'

    @property
    def is_active(self) -> bool:
        return self.state in (self.State.QUEUED, self.State.RUNNING)

    @classmethod
    def reap_interrupted(cls) -> int:
        """Fail runs left in flight by a process that is no longer alive.

        A run lives on a thread, so a reload or a Ctrl-C leaves its row stuck
        in RUNNING for ever — which, now that only one run may hold the slot,
        would block every future run. Called at server start and before a CLI
        run, both of which are moments when nothing can legitimately be
        running yet.
        """
        return cls.objects.filter(
            state__in=(cls.State.QUEUED, cls.State.RUNNING)
        ).update(
            state=cls.State.FAILED,
            finished_at=timezone.now(),
            error='Interrupted — the server stopped while this run was in '
                  'flight. Any outreach row left as "Sending (unconfirmed)" '
                  'needs checking against the portal before you run again.',
        )

    def append_log(self, message: str) -> None:
        """Append one timestamped line, without clobbering concurrent writes.

        Concat, not ``F('log') + line``: SQLite reads ``+`` as numeric
        addition and would quietly blank the log.
        """
        stamp = timezone.localtime().strftime('%H:%M:%S')
        line = f'[{stamp}] {message}\n'
        Run.objects.filter(pk=self.pk).update(
            log=Concat('log', models.Value(line), output_field=models.TextField())
        )
        self.log += line


class Outreach(models.Model):
    """One contact attempt per (profile, candidate) — ever.

    The unique constraint makes the no-double-contact promise a database
    guarantee rather than a convention a mid-run crash could break.

    Because a row is unique, the row is *reused* across runs rather than
    duplicated: a DRY_RUN row is upgraded in place to SENT by a later live
    run, and a FAILED row may be retried. Only SENT is terminal. Without that
    distinction a dry run would permanently blocklist everyone it previewed,
    which is the opposite of what a dry run is for.

    SENDING is the claim the runner stakes *before* it asks the portal to send.
    It exists so the unrecoverable failure mode is "recorded, possibly not
    sent" rather than "sent, not recorded" — the first costs one person a
    message they never got, the second sends them a second one.
    """

    class Status(models.TextChoices):
        DRY_RUN = 'DRY_RUN', 'Dry run (nothing sent)'
        SENDING = 'SENDING', 'Sending (unconfirmed)'
        SENT = 'SENT', 'Sent'
        SKIPPED = 'SKIPPED', 'Skipped'
        FAILED = 'FAILED', 'Failed'

    #: Never overwritten once written.
    TERMINAL = frozenset({Status.SENT})
    #: Statuses that mean "never touch this person again for this profile".
    #: A claim blocks too: if we cannot prove a message did not go out, we
    #: must assume it did.
    BLOCKING = frozenset({Status.SENT, Status.SENDING})
    #: Claims a human still has to resolve against the portal.
    UNRESOLVED = frozenset({Status.SENDING})

    search_profile = models.ForeignKey(
        SearchProfile, on_delete=models.CASCADE, related_name='outreach'
    )
    candidate = models.ForeignKey(
        Candidate, on_delete=models.CASCADE, related_name='outreach'
    )
    run = models.ForeignKey(
        Run, null=True, blank=True, on_delete=models.SET_NULL, related_name='outreach'
    )
    template = models.ForeignKey(
        MessageTemplate, null=True, blank=True, on_delete=models.SET_NULL
    )
    status = models.CharField(max_length=12, choices=Status.choices)
    sent_at = models.DateTimeField(null=True, blank=True)
    detail = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-id']
        constraints = [
            models.UniqueConstraint(
                fields=['search_profile', 'candidate'],
                name='unique_outreach_per_profile_candidate',
            )
        ]

    def __str__(self):
        return f'{self.candidate} — {self.get_status_display()}'

    @classmethod
    def already_contacted_ids(cls, profile) -> set[str]:
        """External ids this profile must never contact again.

        Fetched once per run and held in memory: one query instead of one per
        candidate, and it cannot drift mid-run.
        """
        return set(
            cls.objects.filter(
                search_profile=profile, status__in=cls.BLOCKING
            ).values_list('candidate__external_id', flat=True)
        )
