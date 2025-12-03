import json
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings
from django.core.mail import send_mail
from django.http import JsonResponse, Http404, HttpRequest
from django.shortcuts import render
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import (
    PrivacyPolicy,
    TermsOfService,
    CookiesPolicy,
    ContactEnquiry,
    ContactRecipientEmail,
    Lead,
)


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


def product_detail(request, product_id: str):
    """
    Detail page: fetch a single product from the external API and render
    the existing `product_detail.html` template.
    """
    product_id = (product_id or "").strip()
    if not product_id:
        raise Http404("Product not found")

    # Call the upstream detail endpoint using the product UUID / ID.
    try:
        upstream = _call_globesuggest_api(
            f"/globesuggest/api/products/{urllib.parse.quote(product_id)}/"
        )
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise Http404("Product not found")
        raise Http404("Unable to load product at this time")
    except Exception:
        raise Http404("Unable to load product at this time")

    raw_product = upstream.get("data") or upstream

    # Light normalisation so existing template fields have something to show.
    product = {
        **raw_product,
        "product_title": raw_product.get("product_title")
        or raw_product.get("product_name")
        or raw_product.get("name")
        or "",
        "short_description": raw_product.get("short_description")
        or raw_product.get("description")
        or "",
        # Ensure templates can always link back to this detail page by ID.
        "product_id": raw_product.get("product_id") or product_id,
        "id": raw_product.get("id") or raw_product.get("product_id") or product_id,
    }

    # Minimal JSON-LD; you can extend this as needed.
    product_schema = json.dumps(
        {
            "@context": "https://schema.org",
            "@type": "Product",
            "name": product.get("product_title"),
            "description": product.get("short_description"),
            "image": product.get("image") or product.get("image_url"),
        }
    )

    return render(
        request,
        "product_detail.html",
        {
            "product": product,
            "product_schema": product_schema,
        },
    )


def blog_detail(request, product_id: str, blog_index: int):
    """
    Detail page for a single blog entry associated with a product.
    Reuses the product detail API and picks the requested blog from
    the product's `blog_posts` collection.
    """
    product_id = (product_id or "").strip()
    if not product_id:
        raise Http404("Product not found")

    try:
        upstream = _call_globesuggest_api(
            f"/globesuggest/api/products/{urllib.parse.quote(product_id)}/"
        )
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise Http404("Product not found")
        raise Http404("Unable to load product at this time")
    except Exception:
        raise Http404("Unable to load product at this time")

    raw_product = upstream.get("data") or upstream

    product = {
        **raw_product,
        "product_title": raw_product.get("product_title")
        or raw_product.get("product_name")
        or raw_product.get("name")
        or "",
        "short_description": raw_product.get("short_description")
        or raw_product.get("description")
        or "",
        "product_id": raw_product.get("product_id") or product_id,
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
    quantity_raw = payload.get("quantity")
    quantity: int | None
    try:
        quantity = int(quantity_raw) if quantity_raw not in (None, "",) else None
        if quantity is not None and quantity <= 0:
            quantity = None
    except (TypeError, ValueError):
        quantity = None

    frequency = str(payload.get("frequency") or "").strip()
    if not (mobile or quantity or frequency):
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
    if not mobile:
        return JsonResponse(
            {"status": "error", "message": "Please enter your mobile number."},
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