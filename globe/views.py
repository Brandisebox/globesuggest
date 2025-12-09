import base64
import json
import re
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings
from django.core.mail import send_mail
from django.http import JsonResponse, Http404, HttpRequest
from django.shortcuts import render
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.text import slugify
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

import logging

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .models import (
    PrivacyPolicy,
    TermsOfService,
    CookiesPolicy,
    ContactEnquiry,
    ContactRecipientEmail,
    Lead,
    ServiceTab,
    ServiceTabPoint,
    AnalyticsSession,
    AnalyticsEvent,
)
from .schema_utils import build_product_schema


logger = logging.getLogger(__name__)
_LOCAL_ANALYTICS_PRIVATE_KEY = None


def _get_local_analytics_private_key():
    """
    Lazily load and cache the RSA private key used to decrypt analytics
    envelopes sent to the local `/api/analytics/ingest/` endpoint.

    The corresponding public key is exposed to the frontend via the
    context processor, while this private key must only be provided
    via environment variable and never rendered in templates.
    """

    global _LOCAL_ANALYTICS_PRIVATE_KEY
    if _LOCAL_ANALYTICS_PRIVATE_KEY is not None:
        return _LOCAL_ANALYTICS_PRIVATE_KEY

    pem = getattr(settings, "ANALYTICS_LOCAL_PRIVATE_KEY_PEM", "") or ""
    if not pem.strip():
        _LOCAL_ANALYTICS_PRIVATE_KEY = None
        return None

    try:
        _LOCAL_ANALYTICS_PRIVATE_KEY = serialization.load_pem_private_key(
            pem.encode("utf-8"),
            password=None,
        )
    except Exception:
        _LOCAL_ANALYTICS_PRIVATE_KEY = None
    return _LOCAL_ANALYTICS_PRIVATE_KEY


def _decrypt_analytics_envelope(body: dict) -> dict | None:
    """
    Decrypt a hybrid RSA-OAEP + AES-GCM envelope emitted by the frontend
    tracker into the original JSON payload:

        {
          "alg": "RSA-OAEP/AES-GCM",
          "key": "<base64 AES key encrypted with RSA>",
          "iv": "<base64 IV>",
          "data": "<base64 ciphertext>"
        }
    """

    if not isinstance(body, dict):
        return None

    key_b64 = body.get("key")
    iv_b64 = body.get("iv")
    data_b64 = body.get("data")
    if not (key_b64 and iv_b64 and data_b64):
        return None

    private_key = _get_local_analytics_private_key()
    if private_key is None:
        # Local private key not configured; caller may choose to ignore.
        return None

    try:
        enc_key = base64.b64decode(key_b64)
        iv = base64.b64decode(iv_b64)
        ciphertext = base64.b64decode(data_b64)

        aes_key = private_key.decrypt(
            enc_key,
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        )
        aesgcm = AESGCM(aes_key)
        plaintext = aesgcm.decrypt(iv, ciphertext, None)
        return json.loads(plaintext.decode("utf-8"))
    except Exception:
        return None


def home(request):
    """
    Render the main search page.
    """
    return render(request, "home.html")


def test_api(request):
    """
    Render the standalone API testing dashboard.
    """
    return render(request, "test_api.html")


def _call_globesuggest_api(path: str, params: dict | None = None) -> dict:
    """
    Low-level helper to call the external 1Matrix / Globesuggest API.
    Keeps base URL and API key in settings / env.
    """
    base = (settings.GLOBESUGGEST_API_BASE or "").rstrip("/")
    if not base:
        raise RuntimeError("GLOBESUGGEST_API_BASE is not configured")

    query = ""
    if params:
        # Safe encoding / sanitisation for query params
        query = "?" + urllib.parse.urlencode(params, doseq=True, safe=" ")

    url = f"{base}/{path.lstrip('/')}{query}"

    headers = {
        "Accept": "application/json",
        # Present a stable origin for upstream CORS / auth checks
        "Origin": "https://globesuggest.com",
    }
    api_key = getattr(settings, "GLOBESUGGEST_API_KEY", "") or ""
    if api_key:
        # Primary auth header expected by the 1Matrix / Globesuggest API
        headers["X-GLOBESUGGEST-API-KEY"] = api_key
        # Additional header explicitly requested ("X-GLOBE")
        headers["X-GLOBE"] = api_key

    req = urllib.request.Request(url, headers=headers, method="GET")

    with urllib.request.urlopen(req, timeout=6) as resp:
        data = resp.read()

    try:
        return json.loads(data.decode("utf-8"))
    except json.JSONDecodeError:
        raise RuntimeError("Invalid JSON response from Globesuggest API")


def api_search_suggest(request):
    """
    AJAX endpoint used by home.html to fetch product suggestions.
    Adapts the external API response into a compact, frontend-friendly shape.
    """
    q = (request.GET.get("q") or "").strip()

    # Mirror frontend behaviour; very short queries don't hit the network.
    if len(q) < 2:
        return JsonResponse({"results": []})

    # Basic query sanitisation / length limiting
    if len(q) > 120:
        q = q[:120]

    try:
        upstream = _call_globesuggest_api(
            "/globesuggest/api/products/search/",
            params={"q": q},
        )
    except urllib.error.HTTPError as exc:
        # Surface upstream status to Django logs for easier debugging.
        print(
            "Globesuggest search HTTPError",
            exc.code,
            getattr(exc, "reason", ""),
            getattr(exc, "url", ""),
        )
        return JsonResponse(
            {
                "results": [],
                "error": f"Upstream search error (status {exc.code}).",
            },
            status=exc.code,
        )
    except urllib.error.URLError as exc:
        print("Globesuggest search URLError", getattr(exc, "reason", exc))
        return JsonResponse(
            {"results": [], "error": "Unable to reach product search service."},
            status=502,
        )
    except Exception as exc:
        print("Globesuggest search unexpected error", repr(exc))
        return JsonResponse(
            {"results": [], "error": "Unexpected error calling search service."},
            status=502,
        )

    raw_items = upstream.get("data") or upstream.get("results") or []

    results: list[dict] = []
    for item in raw_items:
        name = (
            item.get("product_name")
            or item.get("product_title")
            or item.get("name")
            or ""
        )
        if not name:
            continue

        raw_slug = item.get("product_slug") or item.get("slug") or ""
        if raw_slug:
            slug = str(raw_slug).strip()
        else:
            # Fallback: derive a slug from the name + id to keep it unique/stable
            pid = str(item.get("product_id") or "").strip()
            base_slug = slugify(name)
            slug = f"{base_slug}-{pid}" if pid else base_slug

        if not slug:
            continue

        results.append(
            {
                # Shape tailored for the existing home.html JS
                "id": item.get("product_id"),
                "slug": slug,
                "title": name,
                "country": item.get("country") or "",
                "image": item.get("image") or item.get("image_url") or "",
            }
        )

        if len(results) >= 6:
            break

    return JsonResponse({"results": results})


_UUID_LIKE_RE = re.compile(r"^[0-9a-fA-F\-]{20,64}$")


def _looks_like_product_id(value: str) -> bool:
    """
    Heuristic to decide whether a URL segment is likely a UUID / product_id
    (e.g. 'd144f303-05ae-49f1-bab8-1e1f6cfb3c21') versus an SEO slug
    ('dropper-bottle-supplier').
    """

    if not value:
        return False
    return bool(_UUID_LIKE_RE.match(value))


def _resolve_product_id_from_slug(slug: str) -> str | None:
    """
    Best-effort resolution from a product_slug to the upstream product_id.

    The external API only exposes the detail endpoint by product_id
    (UUID-style identifier), but search responses include both slug and ID.
    We call the search endpoint with the slug as the query and then pick
    the first exact slug match to recover the product_id.
    """

    slug = (slug or "").strip().strip("/")
    if not slug:
        return None

    # Normalise once for comparison.
    target_slug = slugify(slug) or slug

    # Try a few different query shapes in case the upstream search does not
    # index the raw slug string directly.
    candidates: list[str] = []
    candidates.append(slug)  # e.g. "dropper-bottle-supplier"
    hyphen_to_space = slug.replace("-", " ")
    if hyphen_to_space != slug:
        candidates.append(hyphen_to_space)  # "dropper bottle supplier"

    words = hyphen_to_space.split()
    if words:
        # Shorter phrases often work better for search endpoints.
        if len(words) >= 2:
            candidates.append(" ".join(words[:2]))
        candidates.append(words[0])

    seen_queries: set[str] = set()

    for q in candidates:
        q = q.strip()
        if not q or q in seen_queries:
            continue
        seen_queries.add(q)

        try:
            search_resp = _call_globesuggest_api(
                "/globesuggest/api/products/search/",
                params={"q": q},
            )
        except Exception:
            # Network / upstream issues for one query shouldn't block others.
            continue

        items = search_resp.get("data") or search_resp.get("results") or []
        if not isinstance(items, (list, tuple)):
            continue

        # Prefer an exact slug match (normalised) from the payload.
        for item in items:
            if not isinstance(item, dict):
                continue
            raw_slug = (
                item.get("product_slug")
                or item.get("slug")
                or ""
            )
            item_slug = slugify(str(raw_slug).strip()) or str(raw_slug).strip()
            if not item_slug:
                continue
            if item_slug != target_slug:
                continue

            pid = (
                item.get("product_id")
                or item.get("id")
                or ""
            )
            pid = str(pid).strip()
            if pid:
                return pid

        # Fallback: if exactly one item is returned and it has an ID, we can
        # safely assume it's the intended product for this slug-derived query.
        if len(items) == 1 and isinstance(items[0], dict):
            pid = (
                items[0].get("product_id")
                or items[0].get("id")
                or ""
            )
            pid = str(pid).strip()
            if pid:
                return pid

    return None


def _fetch_product_by_identifier(identifier: str) -> dict:
    """
    Fetch a product dict from the external API using either:

    - a direct product_id / UUID (preferred when it looks like an ID)
    - or a product_slug, which is resolved to product_id via search

    This keeps the public URLs clean (`/product-slug/`) while still
    calling the upstream detail endpoint by product_id only.
    """

    identifier = (identifier or "").strip()
    if not identifier:
        raise Http404("Product not found")

    # Path 1: looks like a UUID / numeric ID — call the detail endpoint directly.
    if _looks_like_product_id(identifier):
        try:
            upstream = _call_globesuggest_api(
                f"/globesuggest/api/products/{urllib.parse.quote(identifier)}/"
            )
            return upstream.get("data") or upstream
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise Http404("Unable to load product at this time")
            # If the direct lookup fails with a 404, fall back to slug logic below.
        except Exception:
            raise Http404("Unable to load product at this time")

    # Path 2: treat the identifier as a slug and resolve it via search.
    product_id = _resolve_product_id_from_slug(identifier)
    if not product_id:
        raise Http404("Product not found")

    try:
        upstream = _call_globesuggest_api(
            f"/globesuggest/api/products/{urllib.parse.quote(product_id)}/"
        )
        return upstream.get("data") or upstream
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise Http404("Product not found")
        raise Http404("Unable to load product at this time")
    except Exception:
        raise Http404("Unable to load product at this time")


def product_detail(request, product_id: str):
    """
    Detail page: fetch a single product from the external API and render
    the existing `product_detail.html` template.
    """
    # Accept either a raw product_id or an SEO slug in the URL.
    raw_product = _fetch_product_by_identifier(product_id)

    # Light normalisation so existing template fields have something to show.
    product = {
        **raw_product,
        "product_title": (
            raw_product.get("product_title")
            or raw_product.get("product_name")
            or raw_product.get("name")
            or ""
        ),
        "short_description": (
            raw_product.get("short_description")
            or raw_product.get("description")
            or ""
        ),
        # Ensure templates can always link back to this detail page by ID.
        # Ensure templates and analytics can consistently access the UUID.
        "product_id": raw_product.get("product_id") or raw_product.get("id") or product_id,
        "id": raw_product.get("id") or raw_product.get("product_id") or product_id,
    }

    # Normalise FAQ data from API into a template-friendly collection.
    # The upstream payload provides:
    # "faqs": [{"question": "...", "answer": "..."}, ...]
    raw_faqs = raw_product.get("faqs") or raw_product.get("faq") or []
    faqs_formatted: list[dict] = []
    if isinstance(raw_faqs, (list, tuple)):
        for item in raw_faqs:
            if not isinstance(item, dict):
                continue
            question = str(item.get("question") or "").strip()
            answer = str(item.get("answer") or "").strip()
            if not (question and answer):
                continue
            faqs_formatted.append(
                {
                    "question": question,
                    "answer": answer,
                }
            )

    # Expose under the key expected by `product_detail.html`.
    if faqs_formatted:
        product["faqs_formatted"] = faqs_formatted

    # Load dynamic "Why / Guarantee / Quality / QC" tabs content from the database.
    # These tabs are global (not product-specific) and rendered in `product_detail.html`.
    tabs_section = (
        ServiceTab.objects.filter(is_active=True)
        .order_by("order", "tab_name")
        .prefetch_related("points")
    )

    # Rich JSON-LD graph for all major sections (product, offers, videos, FAQs, etc.).
    # Kept behind a dedicated utility so the template simply renders a single script
    # block and the backend stays responsible for generating valid absolute URLs.
    product_schema = build_product_schema(request, product)

    return render(
        request,
        "product_detail.html",
        {
            "product": product,
            "product_schema": product_schema,
            "tabs_section": tabs_section,
        },
    )


def blog_detail(request, product_id: str, blog_index: int):
    """
    Detail page for a single blog entry associated with a product.
    Reuses the product detail API and picks the requested blog from
    the product's `blog_posts` collection.
    """
    # Accept either a raw product_id or an SEO slug in the URL.
    raw_product = _fetch_product_by_identifier(product_id)

    product = {
        **raw_product,
        "product_title": raw_product.get("product_title")
        or raw_product.get("product_name")
        or raw_product.get("name")
        or "",
        "short_description": raw_product.get("short_description")
        or raw_product.get("description")
        or "",
        "product_id": raw_product.get("product_id") or raw_product.get("id") or product_id,
        "id": raw_product.get("id") or raw_product.get("product_id") or product_id,
    }

    blog_posts = raw_product.get("blog_posts") or []
    # blog_index in the URL is 1-based to match the template loop counter.
    if not blog_posts or blog_index < 1 or blog_index > len(blog_posts):
        raise Http404("Blog not found")

    blog = blog_posts[blog_index - 1]

    return render(
        request,
        "blog_detail.html",
        {
            "product": product,
            "blog": blog,
            "blog_index": blog_index,
        },
    )


def _get_single_policy_or_404(model):
    """
    Helper to safely fetch the single instance for a policy model.

    If no instance exists (or more than one, in case of manual DB edits),
    we return a clean 404 so the public site does not break.
    """
    try:
        return model.objects.get()
    except model.DoesNotExist:
        raise Http404("Policy not configured.")
    except model.MultipleObjectsReturned:
        raise Http404("Multiple policy records found; please contact support.")


def privacy_policy(request):
    """
    Render the Privacy Policy page with rich HTML content.
    """
    policy = _get_single_policy_or_404(PrivacyPolicy)
    return render(request, "privacy_policy.html", {"policy": policy})


def terms_of_service(request):
    """
    Render the Terms of Service page with rich HTML content.
    """
    policy = _get_single_policy_or_404(TermsOfService)
    return render(request, "terms_of_service.html", {"policy": policy})


def cookies_policy(request):
    """
    Render the Cookies Policy page with rich HTML content.
    """
    policy = _get_single_policy_or_404(CookiesPolicy)
    return render(request, "cookies_policy.html", {"policy": policy})

def about_view(request):
    return render(request, "about.html")


def contact_view(request: HttpRequest):
    """
    Render the public contact page with the enquiry form.
    """
    return render(request, "contact.html")


def _get_client_ip(request: HttpRequest) -> str | None:
    """
    Best-effort extraction of the client's IP address.
    """
    xff = request.META.get("HTTP_X_FORWARDED_FOR")
    if xff:
        # In case of multiple addresses, we take the first one.
        ip = xff.split(",")[0].strip()
        if ip:
            return ip
    return request.META.get("REMOTE_ADDR")


def _parse_json_body(request: HttpRequest) -> dict:
    """
    Safely parse a JSON request body and always return a dict.
    """
    try:
        if not request.body:
            return {}
        data = json.loads(request.body.decode("utf-8"))
        if isinstance(data, dict):
            return data
        return {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}


@require_POST
def contact_api(request: HttpRequest) -> JsonResponse:
    """
    AJAX endpoint used by `contact.html` to submit enquiries.

    - Validates minimal required fields
    - Persists the enquiry to the database
    - Sends copies of the enquiry to all active `ContactRecipientEmail` records
    - Returns JSON suitable for the existing frontend JS handlers
    """

    # Basic guard to ensure this is used as an AJAX endpoint.
    if request.headers.get("X-Requested-With") != "XMLHttpRequest":
        return JsonResponse({"message": "Invalid request."}, status=400)

    name = (request.POST.get("name") or "").strip()
    email = (request.POST.get("email") or "").strip()
    phone = (request.POST.get("phone") or "").strip()
    message = (request.POST.get("message") or "").strip()

    if not (name and email and phone and message):
        return JsonResponse(
            {"message": "Please fill in all required fields before submitting."},
            status=400,
        )

    # Persist enquiry
    enquiry = ContactEnquiry.objects.create(
        name=name,
        email=email,
        phone=phone,
        message=message,
        source_ip=_get_client_ip(request),
        user_agent=(request.META.get("HTTP_USER_AGENT") or "")[:1024],
    )

    # Prepare email dispatch to all active recipients.
    recipients = list(
        ContactRecipientEmail.objects.filter(is_active=True).values_list(
            "email", flat=True
        )
    )

    if recipients:
        subject = f"New contact enquiry from {name}"
        lines = [
            "A new contact enquiry has been submitted on globesuggest:",
            "",
            f"Name   : {name}",
            f"Email  : {email}",
            f"Phone  : {phone}",
            "",
            "Message:",
            message,
            "",
            f"Enquiry ID : {enquiry.id}",
            f"Submitted at : {enquiry.submitted_at:%Y-%m-%d %H:%M:%S}",
        ]
        body = "\n".join(lines)

        # Use a sensible from-email; fall back to the site default if configured.
        from_email = getattr(settings, "DEFAULT_FROM_EMAIL", None) or email

        try:
            send_mail(
                subject,
                body,
                from_email,
                recipients,
                fail_silently=False,
            )
        except Exception as exc:  # pragma: no cover - defensive logging only
            # We log the error but still treat the enquiry as successful,
            # because it has been saved in the database.
            print("Contact enquiry email send failed:", repr(exc))

    return JsonResponse(
        {"message": "Thank you! Your enquiry has been received."},
        status=200,
    )


@csrf_exempt
@require_POST
def enquiry_draft(request: HttpRequest) -> JsonResponse:
    """
    Lightweight endpoint used by `product_detail.html` to autosave partial
    enquiry data as a draft `Lead`.

    The frontend does not rely heavily on the response body; we just do
    a best‑effort upsert keyed by (session_id, product_id).
    """

    payload = _parse_json_body(request)

    # If there's nothing meaningful, treat as a no‑op.
    mobile = str(payload.get("mobile") or "").strip()
    email = str(payload.get("email") or "").strip()
    quantity_raw = payload.get("quantity")
    quantity: int | None
    try:
        quantity = int(quantity_raw) if quantity_raw not in (None, "",) else None
        if quantity is not None and quantity <= 0:
            quantity = None
    except (TypeError, ValueError):
        quantity = None

    frequency = str(payload.get("frequency") or "").strip()
    if not (mobile or email or quantity or frequency):
        return JsonResponse({"status": "ignored"}, status=200)

    session_id = str(payload.get("session_id") or "").strip()
    product_id = str(payload.get("product_id") or "").strip()

    # Normalise product context
    product_slug = str(payload.get("product_slug") or "").strip()
    product_name = str(payload.get("product_name") or "").strip()
    page_url = str(payload.get("page_url") or request.path).strip()

    # Reuse a single draft per (session, product) to avoid unbounded growth.
    lead: Lead | None = None
    if session_id and product_id:
        lead = (
            Lead.objects.filter(
                session_id=session_id,
                product_id=product_id,
                is_draft=True,
            )
            .order_by("-created_at")
            .first()
        )

    if lead is None:
        lead = Lead(
            session_id=session_id,
            product_id=product_id,
            is_draft=True,
            source=Lead.SOURCE_DRAFT,
        )

    # Update fields with latest snapshot
    if product_slug:
        lead.product_slug = product_slug
    if product_name:
        lead.product_name = product_name
    if page_url:
        lead.page_url = page_url

    if mobile:
        lead.mobile = mobile
    if email:
        lead.email = email
    lead.quantity = quantity
    if frequency:
        lead.frequency = frequency

    # Basic request metadata for diagnostics
    lead.source_ip = _get_client_ip(request)
    ua = (request.META.get("HTTP_USER_AGENT") or "")[:1024]
    lead.user_agent = ua

    lead.save(update_fields=None)  # Let Django compute all touched fields.

    return JsonResponse({"status": "success", "lead_id": lead.id}, status=200)


@csrf_exempt
@require_POST
def enquiry_submit(request: HttpRequest) -> JsonResponse:
    """
    Primary endpoint used by:

    - The “Discuss Your Needs” form
    - The Quick Enquiry popup

    Both flows submit JSON here; we create a non‑draft `Lead` instance for
    each explicit submission.
    """

    payload = _parse_json_body(request)

    mobile = str(payload.get("mobile") or "").strip()
    email = str(payload.get("email") or "").strip()
    if not (mobile or email):
        return JsonResponse(
            {
                "status": "error",
                "message": "Please enter your email address or mobile number.",
            },
            status=400,
        )

    quantity_raw = payload.get("quantity")
    try:
        quantity = int(quantity_raw) if quantity_raw not in (None, "",) else None
        if quantity is not None and quantity <= 0:
            quantity = None
    except (TypeError, ValueError):
        quantity = None

    frequency = str(payload.get("frequency") or "").strip()

    product_id = str(payload.get("product_id") or "").strip()
    product_slug = str(payload.get("product_slug") or "").strip()
    product_name = str(payload.get("product_name") or "").strip()
    page_url = str(payload.get("page_url") or request.path).strip()
    session_id = str(payload.get("session_id") or "").strip()

    # Infer source based on the shape of data.
    if quantity is not None or frequency:
        source = Lead.SOURCE_DISCUSS
    else:
        source = Lead.SOURCE_QUICK

    lead = Lead.objects.create(
        session_id=session_id,
        product_id=product_id,
        product_slug=product_slug,
        product_name=product_name,
        quantity=quantity,
        frequency=frequency,
        email=email,
        mobile=mobile,
        page_url=page_url,
        source=source,
        is_draft=False,
        submitted_at=timezone.now(),
        source_ip=_get_client_ip(request),
        user_agent=(request.META.get("HTTP_USER_AGENT") or "")[:1024],
    )

    # Optionally, mark any existing drafts for this session/product as non‑draft
    if session_id and product_id:
        Lead.objects.filter(
            session_id=session_id,
            product_id=product_id,
            is_draft=True,
        ).update(is_draft=False, submitted_at=lead.submitted_at)

    message = (
        "Thanks! We received your enquiry. Our team will contact you shortly."
    )

    return JsonResponse(
        {
            "status": "success",
            "message": message,
            "lead_id": lead.id,
        },
        status=200,
    )


@csrf_exempt
@require_POST
def analytics_ingest(request: HttpRequest) -> JsonResponse:
    """
    Ingest endpoint mirroring the encrypted payload that is sent to 1matrix.io,
    but stored locally in plain JSON for reporting inside this project.

    Frontend now sends an encrypted envelope for the local endpoint, using
    the same hybrid RSA-OAEP + AES-GCM scheme as for 1matrix.io. For
    backwards compatibility, we also accept plain JSON shaped as:

        {"session": {...}, "events": [{...}, ...]}
    """

    raw = _parse_json_body(request)

    # Backwards-compatible: if payload already looks like the final shape,
    # we use it directly; otherwise we attempt decryption.
    if isinstance(raw, dict) and "session" in raw and "events" in raw:
        data = raw
    else:
        data = _decrypt_analytics_envelope(raw) or {}

    if not isinstance(data, dict):
        return JsonResponse(
            {"status": "ignored", "reason": "invalid_payload"},
            status=200,
        )

    session_data = data.get("session") or {}
    events = data.get("events") or []

    # Minimal guard: we need a session_id to store anything meaningful.
    session_id = str(session_data.get("session_id") or "").strip()
    if not session_id:
        return JsonResponse(
            {"status": "ignored", "reason": "missing session_id"},
            status=200,
        )

    product_id = str(session_data.get("product_id") or "").strip()[:64]

    def _parse_ts(value: str | None):
        if not value:
            return None
        dt = parse_datetime(value)
        if dt is None:
            return None
        if timezone.is_naive(dt):
            dt = timezone.make_aware(dt, timezone=timezone.utc)
        return dt

    started_at = _parse_ts(session_data.get("started_at"))
    ended_at = _parse_ts(session_data.get("ended_at"))

    session_obj, created = AnalyticsSession.objects.get_or_create(
        session_id=session_id,
        product_id=product_id,
        defaults={
            "user_id": str(session_data.get("user_id") or "").strip()[:64],
            "started_at": started_at,
            "ended_at": ended_at,
        },
    )

    # Keep session fields in sync with the latest snapshot coming from the client.
    # We purposefully truncate strings to avoid unexpected DB errors.
    session_obj.user_id = str(session_data.get("user_id") or session_obj.user_id or "").strip()[:64]
    session_obj.started_at = session_obj.started_at or started_at
    if ended_at:
        session_obj.ended_at = ended_at

    session_obj.path = (session_data.get("path") or session_obj.path or "")[:500]
    session_obj.traffic_source = (session_data.get("traffic_source") or session_obj.traffic_source or "")[:64]

    session_obj.utm_source = (session_data.get("utm_source") or session_obj.utm_source or "")[:100]
    session_obj.utm_medium = (session_data.get("utm_medium") or session_obj.utm_medium or "")[:100]
    session_obj.utm_campaign = (session_data.get("utm_campaign") or session_obj.utm_campaign or "")[:100]
    session_obj.utm_term = (session_data.get("utm_term") or session_obj.utm_term or "")[:100]
    session_obj.utm_content = (session_data.get("utm_content") or session_obj.utm_content or "")[:100]

    session_obj.device = (session_data.get("device") or session_obj.device or "")[:32]
    session_obj.os = (session_data.get("os") or session_obj.os or "")[:128]
    session_obj.browser = (session_data.get("browser") or session_obj.browser or "")[:255]
    session_obj.viewport = (session_data.get("viewport") or session_obj.viewport or "")[:32]
    session_obj.orientation = (session_data.get("orientation") or session_obj.orientation or "")[:32]
    session_obj.language = (session_data.get("language") or session_obj.language or "")[:32]
    session_obj.country = (session_data.get("country") or session_obj.country or "")[:64]

    session_obj.consent = bool(session_data.get("consent", session_obj.consent))
    session_obj.is_returning = bool(session_data.get("is_returning", session_obj.is_returning))
    session_obj.sampled = bool(session_data.get("sampled", session_obj.sampled))

    def _as_int(name: str, default: int = 0) -> int:
        try:
            return int(session_data.get(name, getattr(session_obj, name, default)) or 0)
        except (TypeError, ValueError):
            return getattr(session_obj, name, default)

    session_obj.max_scroll_pct = max(0, min(100, _as_int("max_scroll_pct")))
    session_obj.cta_clicks = max(0, _as_int("cta_clicks"))
    session_obj.enquiry_submissions = max(0, _as_int("enquiry_submissions"))
    session_obj.video_seconds_watched = max(0, _as_int("video_seconds_watched"))
    session_obj.idle_time_ms = max(0, _as_int("idle_time_ms"))
    session_obj.events_count = max(0, _as_int("events_count"))
    session_obj.duration_ms = max(0, _as_int("duration_ms"))

    section_durations = session_data.get("section_durations")
    if isinstance(section_durations, dict):
        # Merge with any existing durations, summing values per section.
        merged = dict(session_obj.section_durations or {})
        for key, value in section_durations.items():
            try:
                inc = int(value or 0)
            except (TypeError, ValueError):
                inc = 0
            if inc <= 0:
                continue
            merged[key] = int(merged.get(key, 0)) + inc
        session_obj.section_durations = merged

    las = str(session_data.get("last_active_section") or "").strip()
    if las:
        session_obj.last_active_section = las[:100]

    session_obj.save()

    # Persist each raw event for more detailed analysis.
    created_events = 0
    for ev in events:
        if not isinstance(ev, dict):
            continue
        event_type = str(ev.get("event_type") or "").strip()
        if not event_type:
            continue

        occurred_raw = ev.get("occurred_at") or session_data.get("started_at")
        occurred_at = _parse_ts(occurred_raw) or timezone.now()

        page_url = (ev.get("page_url") or session_data.get("path") or "")[:500]
        referrer = (ev.get("referrer") or "")[:500]
        payload = ev.get("payload") or {}
        if not isinstance(payload, dict):
            payload = {"value": payload}

        AnalyticsEvent.objects.create(
            session=session_obj,
            event_type=event_type[:64],
            occurred_at=occurred_at,
            page_url=page_url,
            referrer=referrer,
            payload=payload,
        )
        created_events += 1

    return JsonResponse(
        {
            "status": "ok",
            "session_id": session_obj.session_id,
            "product_id": session_obj.product_id,
            "events_saved": created_events,
        },
        status=200,
    )


@csrf_exempt
@require_POST
def analytics_forward(request: HttpRequest) -> JsonResponse:
    """
    Proxy endpoint used by the frontend instead of calling 1matrix.io
    directly. This keeps the secret API key on the server side while
    still forwarding the encrypted envelope as-is.

    Frontend sends the same envelope shape as before:
        {"alg": "...", "key": "...", "iv": "...", "data": "..."}
    """

    try:
        # Read raw body as text; we treat it as opaque JSON and forward it.
        body_bytes = request.body or b""
        body_str = body_bytes.decode("utf-8")
        # Lightweight validation to avoid proxying garbage.
        try:
            parsed = json.loads(body_str)
        except json.JSONDecodeError:
            logger.warning("analytics_forward: invalid JSON payload")
            return JsonResponse(
                {"status": "error", "reason": "invalid_json"},
                status=400,
            )

        if not isinstance(parsed, dict) or not all(
            k in parsed for k in ("alg", "key", "iv", "data")
        ):
            logger.warning("analytics_forward: missing envelope keys")
            return JsonResponse(
                {"status": "error", "reason": "invalid_envelope"},
                status=400,
            )

        remote_url = getattr(
            settings,
            "ANALYTICS_REMOTE_INGEST_URL",
            getattr(settings, "ANALYTICS_INGEST_URL", ""),
        )
        remote_url = (remote_url or "").strip()
        if not remote_url:
            logger.warning("analytics_forward: remote ingest URL not configured")
            return JsonResponse(
                {"status": "error", "reason": "remote_url_not_configured"},
                status=500,
            )

        api_key = getattr(settings, "GLOBESUGGEST_API_KEY", "") or ""
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            # Present a stable origin for upstream permission checks.
            "Origin": "https://globesuggest.com",
        }
        if api_key:
            headers["X-Globesuggest-Api-Key"] = api_key
            headers["X-GLOBE"] = api_key

        logger.info(
            "analytics_forward: forwarding envelope to %s (len=%s)",
            remote_url,
            len(body_str),
        )

        req = urllib.request.Request(
            remote_url,
            data=body_str.encode("utf-8"),
            headers=headers,
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                upstream_status = resp.status
                # We don't really need the body, but read a small chunk to
                # avoid keeping the connection open unnecessarily.
                _ = resp.read(1024)
        except urllib.error.HTTPError as exc:
            upstream_status = exc.code
            logger.warning(
                "analytics_forward: upstream HTTPError status=%s reason=%s url=%s",
                exc.code,
                getattr(exc, "reason", ""),
                getattr(exc, "url", remote_url),
            )
        except urllib.error.URLError as exc:
            logger.warning(
                "analytics_forward: upstream URLError reason=%s url=%s",
                getattr(exc, "reason", exc),
                remote_url,
            )
            return JsonResponse(
                {"status": "error", "reason": "upstream_unreachable"},
                status=502,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "analytics_forward: unexpected upstream error %r url=%s",
                exc,
                remote_url,
            )
            return JsonResponse(
                {"status": "error", "reason": "upstream_error"},
                status=502,
            )

        return JsonResponse(
            {
                "status": "ok",
                "forwarded_status": upstream_status,
            },
            status=200,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("analytics_forward: fatal error %r", exc)
        return JsonResponse(
            {"status": "error", "reason": "internal_error"},
            status=500,
        )