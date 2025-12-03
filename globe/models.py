from django.db import models
from django.core.exceptions import ValidationError




class ServiceTab(models.Model):
    """
    Configurable "Tabs section" entry for the product detail page.

    Each tab holds a title + description and up to ~5 highlight points that
    are rendered in a cards layout (see ServiceTabPoint).
    """

    tab_name = models.CharField(
        max_length=120,
        help_text="Tab label shown in the navigation and inside the hero area.",
    )
    order = models.PositiveIntegerField(
        default=0,
        help_text="Lower numbers appear first in the tabs navigation."
    )
    is_active = models.BooleanField(
        default=True,
        help_text="Uncheck to hide this tab from the site without deleting it."
    )

    class Meta:
        ordering = ["order", "tab_name"]
        verbose_name = "Tabs section tab"
        verbose_name_plural = "Tabs section tabs"

    def __str__(self):
        return self.tab_name


class ServiceTabPoint(models.Model):
    """
    Individual point displayed inside a ServiceTab.

    Admins can configure up to 5 points per tab (not enforced at DB level,
    but the template will render at most 5).
    """

    tab = models.ForeignKey(
        ServiceTab,
        on_delete=models.CASCADE,
        related_name="points",
    )
    icon = models.ImageField(
        upload_to="tabs/points/",
        blank=True,
        null=True,
        help_text="Upload icon image for this point (e.g. 32x32 PNG/SVG).",
    )
    title = models.CharField(max_length=150)
    description = models.TextField()
    order = models.PositiveIntegerField(
        default=0,
        help_text="Controls the order of points inside a tab."
    )

    class Meta:
        ordering = ["order", "title"]
        verbose_name = "Tabs section point"
        verbose_name_plural = "Tabs section points"

    def __str__(self):
        return f"{self.tab.tab_name} – {self.title}"
        

class BasePolicy(models.Model):
    """
    Abstract base model for legal / policy pages.

    Each concrete subclass should enforce a single-instance constraint to ensure
    only one record exists per policy type.
    """

    name = models.CharField(max_length=255, help_text="Display name of the policy.")
    effective_date = models.DateField(help_text="Date from which this policy takes effect.")
    policy_content = models.TextField(help_text="Full policy body (HTML allowed).")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True

    def clean(self):
        """
        Enforce singleton constraint at the model validation level.

        This ensures only a single object can exist for each concrete policy model.
        """
        super().clean()
        model = self.__class__
        qs = model.objects.all()
        if self.pk:
            qs = qs.exclude(pk=self.pk)
        if qs.exists():
            raise ValidationError("Only one instance of this policy is allowed.")

    def __str__(self) -> str:  # pragma: no cover - simple representation
        return self.name or self.__class__.__name__


class PrivacyPolicy(BasePolicy):
    """
    Singleton model representing the site's Privacy Policy.
    """

    class Meta:
        verbose_name = "Privacy Policy"
        verbose_name_plural = "Privacy Policy"


class TermsOfService(BasePolicy):
    """
    Singleton model representing the site's Terms of Service.
    """

    class Meta:
        verbose_name = "Terms of Service"
        verbose_name_plural = "Terms of Service"


class CookiesPolicy(BasePolicy):
    """
    Singleton model representing the site's Cookies Policy.
    """

    class Meta:
        verbose_name = "Cookies Policy"
        verbose_name_plural = "Cookies Policy"


class ContactEnquiry(models.Model):
    """
    Stores a single contact enquiry submitted from the public contact form.

    This model is intentionally simple and append‑only so that every enquiry
    is preserved for auditing and follow‑up.
    """

    name = models.CharField(max_length=255)
    email = models.EmailField()
    phone = models.CharField(max_length=64)
    message = models.TextField()
    submitted_at = models.DateTimeField(auto_now_add=True)
    source_ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)

    class Meta:
        ordering = ("-submitted_at",)

    def __str__(self) -> str:  # pragma: no cover - simple representation
        return f"{self.name} <{self.email}> ({self.submitted_at:%Y-%m-%d %H:%M})"


class ContactRecipientEmail(models.Model):
    """
    Email addresses that should receive a copy of each contact enquiry.

    Add one record per recipient in the Django admin. All active recipients
    will be emailed whenever a new `ContactEnquiry` is created via the form.
    """

    email = models.EmailField(unique=True)
    name = models.CharField(max_length=255, blank=True)
    is_active = models.BooleanField(
        default=True,
        help_text="Only active recipients will receive contact enquiry emails.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Contact recipient email"
        verbose_name_plural = "Contact recipient emails"
        ordering = ("email",)

    def __str__(self) -> str:  # pragma: no cover - simple representation
        return f"{self.name} <{self.email}>" if self.name else self.email


class Lead(models.Model):
    """
    Captures product‑specific enquiries coming from the product detail page.

    Both the main “Discuss Your Needs” form and the Quick Enquiry popup
    store into this model so all leads are centralised and easy to work with.
    """

    SOURCE_DISCUSS = "discuss"
    SOURCE_QUICK = "quick"
    SOURCE_DRAFT = "draft"

    SOURCE_CHOICES = (
        (SOURCE_DISCUSS, "Discuss form"),
        (SOURCE_QUICK, "Quick enquiry"),
        (SOURCE_DRAFT, "Draft (autosave)"),
    )

    # Session / visitor context
    session_id = models.CharField(
        max_length=64,
        blank=True,
        help_text="Anonymous session identifier used for tying together drafts and submissions.",
    )

    # Product context
    product_id = models.CharField(
        max_length=64,
        blank=True,
        help_text="Upstream product identifier (UUID or slug used with the API).",
    )
    product_slug = models.CharField(
        max_length=255,
        blank=True,
        help_text="SEO slug for the product detail page, when available.",
    )
    product_name = models.CharField(
        max_length=255,
        blank=True,
        help_text="Human‑readable product name at the time of the enquiry.",
    )

    # Lead payload
    quantity = models.PositiveIntegerField(null=True, blank=True)
    frequency = models.CharField(
        max_length=32,
        blank=True,
        help_text="One‑time / repeat or similar purchase frequency indicator.",
    )
    mobile = models.CharField(max_length=32, blank=True)
    page_url = models.CharField(
        max_length=500,
        blank=True,
        help_text="Path or URL where the lead was captured.",
    )

    source = models.CharField(
        max_length=16,
        choices=SOURCE_CHOICES,
        default=SOURCE_DISCUSS,
    )
    is_draft = models.BooleanField(
        default=False,
        help_text="True for autosaved partial data that has not been fully submitted yet.",
    )
    submitted_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp when the user explicitly submitted the enquiry.",
    )

    # Request metadata for light auditing
    source_ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self) -> str:  # pragma: no cover - simple representation
        base = self.product_name or self.product_id or "Lead"
        return f"{base} - {self.mobile or 'unknown'}"
