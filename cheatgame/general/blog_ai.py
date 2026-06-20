import json
import logging
import re
from typing import Any, Dict, List

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

BLOG_AI_DRAFT_VERSION = "blog_ai_draft_v1"
ALLOWED_BLOCK_TYPES = {
    "heading",
    "paragraph",
    "quote",
    "callout",
    "table",
    "faq",
    "cta",
    "image_prompt",
    "checklist",
}
ALLOWED_CALLOUT_VARIANTS = {"info", "warning", "tip"}
ALLOWED_CTA_VARIANTS = {"repair_request", "contact"}
ALLOWED_IMAGE_PLACEMENTS = {"featured", "inline"}
UNSAFE_TEXT_RE = re.compile(
    r"<[^>]+>|javascript:|data:text/html|on[a-z]+\s*=|</?(script|style|iframe|object|embed|form|input|button|svg|math)\b",
    re.IGNORECASE,
)


class BlogAiError(Exception):
    user_message = "تولید پیش‌نویس با مشکل مواجه شد."


class BlogAiConfigurationError(BlogAiError):
    user_message = "تنظیمات دستیار هوشمند کامل نیست. کلید API یا حالت تست فعال نشده است."


class BlogAiProviderError(BlogAiError):
    user_message = "ارتباط با سرویس هوش مصنوعی برقرار نشد. لطفاً بعداً دوباره تلاش کنید."


class BlogAiValidationError(BlogAiError):
    user_message = "خروجی دستیار هوشمند معتبر نبود و برای امنیت نمایش داده نشد."

    def __init__(self, errors: List[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _ensure_safe_text(errors: List[str], path: str, value: Any, max_length: int = 4000) -> str:
    text = _clean_text(value)
    if len(text) > max_length:
        errors.append(f"{path} بیش از حد مجاز طولانی است.")
    if text and UNSAFE_TEXT_RE.search(text):
        errors.append(f"{path} شامل HTML خام یا محتوای ناامن است.")
    return text[:max_length]


def _required_text(errors: List[str], path: str, value: Any, max_length: int = 4000) -> str:
    text = _ensure_safe_text(errors, path, value, max_length)
    if not text:
        errors.append(f"{path} الزامی است.")
    return text


def _safe_url(errors: List[str], path: str, value: Any) -> str:
    url = _ensure_safe_text(errors, path, value, 500)
    if not url:
        errors.append(f"{path} الزامی است.")
        return ""
    if url.startswith("/"):
        return url
    if url.startswith("http://") or url.startswith("https://"):
        return url
    errors.append(f"{path} معتبر نیست.")
    return ""


def _validate_block(block: Any, index: int, errors: List[str]) -> Dict[str, Any]:
    if not isinstance(block, dict):
        errors.append(f"بلاک {index + 1} ساختار معتبر ندارد.")
        return {}

    block_type = block.get("type")
    if block_type not in ALLOWED_BLOCK_TYPES:
        errors.append(f"نوع بلاک {index + 1} مجاز نیست.")
        return {}

    if block_type == "heading":
        level = block.get("level")
        if level not in (2, 3, 4):
            errors.append(f"بلاک {index + 1}: سطح تیتر باید ۲، ۳ یا ۴ باشد.")
            level = 2
        return {
            "type": "heading",
            "level": level,
            "text": _required_text(errors, f"بلاک {index + 1}: متن تیتر", block.get("text"), 180),
        }

    if block_type == "paragraph":
        return {
            "type": "paragraph",
            "text": _required_text(errors, f"بلاک {index + 1}: متن پاراگراف", block.get("text")),
        }

    if block_type == "quote":
        return {
            "type": "quote",
            "text": _required_text(errors, f"بلاک {index + 1}: متن نقل قول", block.get("text"), 900),
        }

    if block_type == "callout":
        variant = block.get("variant") or "info"
        if variant not in ALLOWED_CALLOUT_VARIANTS:
            errors.append(f"بلاک {index + 1}: نوع باکس تاکیدی معتبر نیست.")
            variant = "info"
        return {
            "type": "callout",
            "variant": variant,
            "title": _ensure_safe_text(errors, f"بلاک {index + 1}: عنوان باکس", block.get("title") or "نکته مهم", 120),
            "body": _required_text(errors, f"بلاک {index + 1}: متن باکس", block.get("body"), 1200),
        }

    if block_type == "table":
        headers = block.get("headers")
        rows = block.get("rows")
        if not isinstance(headers, list) or not 1 <= len(headers) <= 6:
            errors.append(f"بلاک {index + 1}: جدول باید بین ۱ تا ۶ ستون داشته باشد.")
            headers = []
        if not isinstance(rows, list) or not 1 <= len(rows) <= 12:
            errors.append(f"بلاک {index + 1}: جدول باید بین ۱ تا ۱۲ ردیف داشته باشد.")
            rows = []
        clean_headers = [
            _required_text(errors, f"بلاک {index + 1}: عنوان ستون", header, 120)
            for header in headers[:6]
        ]
        clean_rows = []
        for row_index, row in enumerate(rows[:12]):
            if not isinstance(row, list):
                errors.append(f"بلاک {index + 1}: ردیف {row_index + 1} معتبر نیست.")
                continue
            if len(row) != len(clean_headers):
                errors.append(f"بلاک {index + 1}: تعداد خانه‌های ردیف {row_index + 1} با ستون‌ها برابر نیست.")
            clean_rows.append([
                _ensure_safe_text(errors, f"بلاک {index + 1}: خانه جدول", cell, 500)
                for cell in row[:6]
            ])
        return {"type": "table", "headers": clean_headers, "rows": clean_rows}

    if block_type == "faq":
        items = block.get("items")
        if not isinstance(items, list) or not 1 <= len(items) <= 8:
            errors.append(f"بلاک {index + 1}: سوالات متداول باید بین ۱ تا ۸ آیتم داشته باشد.")
            items = []
        clean_items = []
        for item_index, item in enumerate(items[:8]):
            item = item if isinstance(item, dict) else {}
            clean_items.append({
                "question": _required_text(errors, f"بلاک {index + 1}: سوال {item_index + 1}", item.get("question"), 220),
                "answer": _required_text(errors, f"بلاک {index + 1}: پاسخ {item_index + 1}", item.get("answer"), 1200),
            })
        return {"type": "faq", "items": clean_items}

    if block_type == "cta":
        variant = block.get("variant") or "repair_request"
        if variant not in ALLOWED_CTA_VARIANTS:
            errors.append(f"بلاک {index + 1}: نوع دعوت به اقدام معتبر نیست.")
            variant = "repair_request"
        return {
            "type": "cta",
            "variant": variant,
            "title": _ensure_safe_text(errors, f"بلاک {index + 1}: عنوان CTA", block.get("title"), 160),
            "body": _ensure_safe_text(errors, f"بلاک {index + 1}: متن CTA", block.get("body"), 500),
            "label": _ensure_safe_text(errors, f"بلاک {index + 1}: دکمه CTA", block.get("label"), 80),
        }

    if block_type == "image_prompt":
        placement = block.get("placement") or "inline"
        if placement not in ALLOWED_IMAGE_PLACEMENTS:
            errors.append(f"بلاک {index + 1}: جایگاه تصویر معتبر نیست.")
            placement = "inline"
        return {
            "type": "image_prompt",
            "placement": placement,
            "prompt": _required_text(errors, f"بلاک {index + 1}: پرامپت تصویر", block.get("prompt"), 1200),
            "alt": _ensure_safe_text(errors, f"بلاک {index + 1}: متن جایگزین تصویر", block.get("alt"), 180),
            "caption": _ensure_safe_text(errors, f"بلاک {index + 1}: زیرنویس تصویر", block.get("caption"), 220),
        }

    if block_type == "checklist":
        items = block.get("items")
        if not isinstance(items, list) or not 1 <= len(items) <= 12:
            errors.append(f"بلاک {index + 1}: چک‌لیست باید بین ۱ تا ۱۲ آیتم داشته باشد.")
            items = []
        return {
            "type": "checklist",
            "title": _ensure_safe_text(errors, f"بلاک {index + 1}: عنوان چک‌لیست", block.get("title"), 160),
            "items": [
                _required_text(errors, f"بلاک {index + 1}: آیتم چک‌لیست", item, 280)
                for item in items[:12]
            ],
        }

    errors.append(f"بلاک {index + 1} پشتیبانی نمی‌شود.")
    return {}


def validate_blog_ai_draft_payload(payload: Any) -> Dict[str, Any]:
    errors: List[str] = []
    if not isinstance(payload, dict):
        raise BlogAiValidationError(["خروجی دستیار باید JSON object باشد."])

    if payload.get("version") != BLOG_AI_DRAFT_VERSION:
        errors.append("نسخه خروجی دستیار معتبر نیست.")

    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    title = _required_text(errors, "عنوان مقاله", meta.get("title"), 200)
    seo_title = _ensure_safe_text(errors, "عنوان سئو", meta.get("seo_title"), 200)
    meta_description = _ensure_safe_text(errors, "توضیحات متا", meta.get("meta_description"), 320)
    slug_suggestion = _ensure_safe_text(errors, "اسلاگ پیشنهادی", meta.get("slug_suggestion"), 300)

    outline_source = payload.get("outline") if isinstance(payload.get("outline"), list) else []
    outline = []
    for index, item in enumerate(outline_source[:24]):
        item = item if isinstance(item, dict) else {}
        level = item.get("level")
        if level not in (2, 3, 4):
            errors.append(f"آیتم {index + 1} ساختار مقاله باید سطح ۲، ۳ یا ۴ داشته باشد.")
            level = 2
        outline.append({
            "level": level,
            "title": _required_text(errors, f"عنوان ساختار {index + 1}", item.get("title"), 180),
        })

    blocks_source = payload.get("blocks") if isinstance(payload.get("blocks"), list) else []
    if not blocks_source:
        errors.append("خروجی دستیار باید حداقل یک بلاک داشته باشد.")
    blocks = [
        block
        for block in (_validate_block(block, index, errors) for index, block in enumerate(blocks_source[:40]))
        if block
    ]

    image_prompt_source = payload.get("image_prompts") if isinstance(payload.get("image_prompts"), list) else []
    image_prompts = []
    for index, item in enumerate(image_prompt_source[:8]):
        item = item if isinstance(item, dict) else {}
        placement = item.get("placement") if item.get("placement") in ALLOWED_IMAGE_PLACEMENTS else "inline"
        image_prompts.append({
            "placement": placement,
            "prompt": _required_text(errors, f"پرامپت تصویر {index + 1}", item.get("prompt"), 1200),
            "alt": _ensure_safe_text(errors, f"متن جایگزین تصویر {index + 1}", item.get("alt"), 180),
            "caption": _ensure_safe_text(errors, f"زیرنویس تصویر {index + 1}", item.get("caption"), 220),
        })

    internal_link_source = (
        payload.get("internal_link_suggestions")
        if isinstance(payload.get("internal_link_suggestions"), list)
        else []
    )
    internal_link_suggestions = []
    for index, item in enumerate(internal_link_source[:12]):
        item = item if isinstance(item, dict) else {}
        internal_link_suggestions.append({
            "label": _required_text(errors, f"عنوان لینک داخلی {index + 1}", item.get("label"), 160),
            "url": _safe_url(errors, f"لینک داخلی {index + 1}", item.get("url")),
            "reason": _ensure_safe_text(errors, f"دلیل لینک داخلی {index + 1}", item.get("reason"), 400),
        })

    if errors:
        raise BlogAiValidationError(errors)

    return {
        "version": BLOG_AI_DRAFT_VERSION,
        "meta": {
            "title": title,
            "seo_title": seo_title,
            "meta_description": meta_description,
            "slug_suggestion": slug_suggestion,
        },
        "outline": outline,
        "blocks": blocks,
        "image_prompts": image_prompts,
        "internal_link_suggestions": internal_link_suggestions,
    }


def build_blog_ai_system_prompt() -> str:
    allowed_blocks = ", ".join(sorted(ALLOWED_BLOCK_TYPES))
    return (
        "You are the senior Persian SEO content editor for CheatsGame, an expert gaming repair brand. "
        "Return only valid JSON, no markdown wrapper. "
        f"The JSON version must be {BLOG_AI_DRAFT_VERSION}. Allowed block types are: {allowed_blocks}. "
        "Do not return HTML, iframe, script, style, markdown, or unsafe URLs. "
        "Write natural, fluent Persian for Iranian gamers: professional, clear, trustworthy, slightly energetic, "
        "not robotic, not generic, and not keyword-stuffed. "
        "The draft should feel close to publishable after light human editing, not like an outline. "
        "Target 900 to 1400 Persian words when possible. Use 6 to 8 useful main H2 sections. "
        "Each H2 should be followed by 2 to 4 practical, readable paragraphs unless another block is a better fit. "
        "For repair/service topics, cover: what the problem means, common symptoms, possible causes, what the user should not do, "
        "when professional repair is needed, how CheatsGame handles the inspection, FAQ, and CTA. "
        "Use the primary keyword naturally in the title, SEO title, first section, and meta description; avoid keyword stuffing. "
        "Meta description should usually be around 130 to 160 Persian characters. "
        "Slug suggestion should be latin lowercase kebab-case when possible. "
        "FAQ must include at least 5 real search-intent questions. "
        "Use callout, checklist, FAQ, CTA, and table only when useful. Include one repair CTA near the middle and one near the end. "
        "Image prompts should describe useful real editorial artwork and include practical alt text. "
        "Never claim guaranteed repair, exact price, or certain diagnosis. "
        "For repair content, say final diagnosis and final cost require specialist inspection. "
        "Never publish anything directly."
    )


def build_blog_ai_user_prompt(input_data: Dict[str, Any]) -> str:
    return json.dumps(
        {
            "task": "Generate a safe structured blog draft for CheatsGame.",
            "required_schema": {
                "version": BLOG_AI_DRAFT_VERSION,
                "meta": {
                    "title": "string",
                    "seo_title": "string",
                    "meta_description": "string",
                    "slug_suggestion": "string",
                },
                "outline": [{"level": 2, "title": "string"}],
                "blocks": [
                    {"type": "heading", "level": 2, "text": "string"},
                    {"type": "paragraph", "text": "string"},
                    {"type": "faq", "items": [{"question": "string", "answer": "string"}]},
                    {"type": "cta", "variant": "repair_request"},
                ],
                "image_prompts": [{"placement": "featured", "prompt": "string", "alt": "string"}],
                "internal_link_suggestions": [{"label": "string", "url": "/Repair", "reason": "string"}],
            },
            "editorial_quality_rules": {
                "target_words": "900-1400 Persian words if possible",
                "main_sections": "6-8 H2 sections with useful paragraphs",
                "faq_count": "at least 5 questions",
                "cta_count": "one CTA near the middle and one near the end",
                "tone": "senior Persian SEO editor, expert gaming repair brand, honest and helpful",
                "forbidden_claims": [
                    "guaranteed repair",
                    "exact price guarantee",
                    "certain diagnosis without inspection",
                ],
                "seo": [
                    "primary keyword in title",
                    "primary keyword in SEO title",
                    "primary keyword in first section",
                    "primary keyword in meta description",
                    "natural usage, no stuffing",
                    "latin lowercase kebab-case slug when possible",
                ],
            },
            "input": input_data,
        },
        ensure_ascii=False,
    )


class BlogAiProvider:
    name = "base"

    def generate(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError


def _slugify_repair_topic(topic: str, primary_keyword: str) -> str:
    source = f"{primary_keyword} {topic}".lower()
    if "دریفت" in source or "drift" in source:
        parts = ["ps5" if "ps5" in source else "controller", "drift", "repair"]
    elif "دما" in source or "حرارت" in source or "too hot" in source or "temperature" in source:
        parts = ["ps5", "temperature", "error"]
    elif "تصویر" in source or "no display" in source:
        parts = ["ps5", "no", "display", "repair"]
    elif "hdmi" in source:
        parts = ["ps5", "hdmi", "port", "repair"]
    elif "باتری" in source or "battery" in source:
        parts = ["ps5", "controller", "battery", "repair"]
    else:
        ascii_slug = re.sub(r"[^a-z0-9_-]+", "-", source).strip("-")
        return ascii_slug[:300] or "cheatsgame-repair-guide"
    return "-".join(dict.fromkeys(parts))[:300]


class MockBlogAiProvider(BlogAiProvider):
    name = "mock"

    def generate(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        topic = input_data.get("topic") or "موضوع مقاله"
        primary_keyword = input_data.get("primary_keyword") or topic
        slug = _slugify_repair_topic(topic, primary_keyword)
        common_problem = f"{primary_keyword} معمولاً زمانی جدی می‌شود که مشکل چند بار تکرار شود، روی بازی کردن اثر بگذارد یا بعد از راهکارهای ساده همچنان باقی بماند."
        inspection_note = "در چیتس گیم، بررسی اولیه برای جدا کردن نشانه‌های نرم‌افزاری، مصرفی و سخت‌افزاری انجام می‌شود و هزینه نهایی فقط بعد از بررسی تخصصی اعلام می‌شود."
        return {
            "version": BLOG_AI_DRAFT_VERSION,
            "meta": {
                "title": f"راهنمای {topic}",
                "seo_title": f"{primary_keyword} | راهنمای چیتس گیم",
                "meta_description": f"راهنمای کامل {primary_keyword} در چیتس گیم؛ نشانه‌ها، علت‌های احتمالی، کارهایی که نباید انجام دهید و زمان مناسب برای بررسی تخصصی.",
                "slug_suggestion": slug[:300],
            },
            "outline": [
                {"level": 2, "title": f"{topic} دقیقاً یعنی چه؟"},
                {"level": 2, "title": "علائم رایج که نباید نادیده بگیرید"},
                {"level": 2, "title": "علت‌های احتمالی مشکل"},
                {"level": 2, "title": "قبل از مراجعه چه کارهایی انجام ندهیم؟"},
                {"level": 2, "title": "چه زمانی بررسی تخصصی لازم است؟"},
                {"level": 2, "title": "چیتس گیم چطور درخواست تعمیر را بررسی می‌کند؟"},
                {"level": 2, "title": "سوالات متداول"},
            ],
            "blocks": [
                {"type": "heading", "level": 2, "text": f"{topic} دقیقاً یعنی چه؟"},
                {
                    "type": "paragraph",
                    "text": f"وقتی درباره {primary_keyword} صحبت می‌کنیم، منظور فقط یک پیام خطا یا یک نشانه ساده نیست؛ معمولاً کاربر با رفتاری روبه‌رو می‌شود که تجربه بازی را به هم می‌زند و باعث می‌شود نتواند با خیال راحت از دستگاه استفاده کند. این مشکل ممکن است آرام‌آرام شروع شود یا ناگهان وسط بازی خودش را نشان بدهد.",
                },
                {
                    "type": "paragraph",
                    "text": f"نکته مهم این است که هر نشانه‌ای به معنی خرابی قطعی یک قطعه نیست. گاهی تنظیمات، گردوغبار، استفاده طولانی، کابل یا حتی شرایط نگهداری می‌تواند ظاهر مشکل را شبیه خرابی سخت‌افزاری کند. برای همین بهتر است {primary_keyword} با نگاه مرحله‌ای بررسی شود، نه با حدس سریع.",
                },
                {"type": "heading", "level": 2, "text": "علائم رایج که نباید نادیده بگیرید"},
                {
                    "type": "paragraph",
                    "text": f"{common_problem} اگر دستگاه یک بار رفتار غیرعادی نشان داد، شاید بتوان آن را زیر نظر گرفت؛ اما وقتی همان نشانه در چند بازی، چند کابل یا چند بار استفاده تکرار می‌شود، بهتر است موضوع را جدی‌تر ببینید.",
                },
                {
                    "type": "paragraph",
                    "text": "کاربرهای گیمینگ معمولاً خیلی زود متوجه تغییرهای کوچک می‌شوند؛ کند شدن واکنش، قطع‌ووصلی، داغ شدن غیرعادی، خطاهای تکرارشونده یا عملکردی که فقط گاهی درست است. همین جزئیات برای تکنسین ارزشمند است، چون مسیر عیب‌یابی را کوتاه‌تر می‌کند.",
                },
                {"type": "heading", "level": 2, "text": "علت‌های احتمالی مشکل"},
                {
                    "type": "paragraph",
                    "text": f"علت {primary_keyword} می‌تواند از چند بخش مختلف باشد. در بعضی موارد مشکل از قطعه مصرفی یا فرسوده است، در بعضی موارد از فشار فیزیکی یا ضربه، و گاهی از شرایطی مثل دما، رطوبت، آلودگی یا استفاده طولانی بدون سرویس. بدون بازبینی دقیق، گفتن علت قطعی کار درستی نیست.",
                },
                {
                    "type": "paragraph",
                    "text": "بهترین مسیر این است که نشانه‌ها را دقیق یادداشت کنید: مشکل از چه زمانی شروع شد، در چه شرایطی تکرار می‌شود، آیا با کابل یا دسته دیگر هم اتفاق می‌افتد، و آیا قبل از آن دستگاه ضربه خورده یا باز شده است. این اطلاعات کمک می‌کند بررسی تخصصی دقیق‌تر و سریع‌تر انجام شود.",
                },
                {
                    "type": "callout",
                    "variant": "warning",
                    "title": "تشخیص قطعی فقط بعد از بررسی تخصصی",
                    "body": "هیچ نشانه‌ای به‌تنهایی برای اعلام نتیجه قطعی یا هزینه قطعی کافی نیست. نتیجه نهایی تعمیر بعد از بررسی دستگاه اعلام می‌شود.",
                },
                {"type": "heading", "level": 2, "text": "قبل از مراجعه چه کارهایی انجام ندهیم؟"},
                {
                    "type": "paragraph",
                    "text": "اگر مشکل جدی یا تکرارشونده است، باز کردن دستگاه در خانه، فشار دادن قطعات، استفاده از اسپری‌های نامناسب یا تست با شارژر و کابل غیراستاندارد می‌تواند آسیب را بیشتر کند. بعضی خرابی‌ها با یک حرکت اشتباه از یک تعمیر ساده به یک تعمیر پرریسک‌تر تبدیل می‌شوند.",
                },
                {
                    "type": "paragraph",
                    "text": "همچنین بهتر است قبل از تحویل، اطلاعات مهم، لوازم جانبی همراه و توضیح دقیق مشکل را آماده کنید. توضیح کوتاه اما دقیق، مثل «بعد از نیم ساعت بازی این خطا ظاهر می‌شود» یا «در بازی‌های سنگین بیشتر رخ می‌دهد»، از جمله اطلاعاتی است که واقعاً به روند بررسی کمک می‌کند.",
                },
                {
                    "type": "checklist",
                    "title": "قبل از ثبت درخواست تعمیر آماده کنید",
                    "items": [
                        "مدل دقیق دستگاه یا دسته را مشخص کنید",
                        "زمان و شرایط تکرار مشکل را یادداشت کنید",
                        "اگر کابل، شارژر یا لوازم جانبی خاصی در مشکل نقش دارد همراه داشته باشید",
                        "از باز کردن دستگاه بدون ابزار و تجربه کافی خودداری کنید",
                        "در فرم تعمیر، توضیح کوتاه و دقیق بنویسید",
                    ],
                },
                {
                    "type": "cta",
                    "variant": "repair_request",
                    "title": "می‌خواهید دستگاه بررسی شود؟",
                    "body": "اگر مشکل تکرار می‌شود، درخواست تعمیر ثبت کنید تا تیم چیتس گیم آن را مرحله‌به‌مرحله بررسی کند.",
                    "label": "ثبت درخواست تعمیر",
                },
                {"type": "heading", "level": 2, "text": "چه زمانی بررسی تخصصی لازم است؟"},
                {
                    "type": "paragraph",
                    "text": f"اگر {primary_keyword} چند بار پشت سر هم تکرار شده، در بازی‌های مختلف دیده می‌شود یا بعد از خاموش و روشن کردن ساده برطرف نمی‌شود، زمان بررسی تخصصی رسیده است. مخصوصاً وقتی مشکل روی عملکرد اصلی دستگاه اثر می‌گذارد، عقب انداختن تعمیر می‌تواند ریسک آسیب بیشتر را بالا ببرد.",
                },
                {
                    "type": "paragraph",
                    "text": "بررسی تخصصی فقط پیدا کردن یک قطعه خراب نیست؛ تکنسین باید مسیر مشکل را پیدا کند، قطعات مرتبط را تست کند و مطمئن شود راه‌حل پیشنهادی واقعاً با نشانه‌های دستگاه هم‌خوانی دارد. همین تفاوت باعث می‌شود تصمیم تعمیر، دقیق‌تر و قابل اعتمادتر باشد.",
                },
                {"type": "heading", "level": 2, "text": "چیتس گیم چطور درخواست تعمیر را بررسی می‌کند؟"},
                {
                    "type": "paragraph",
                    "text": inspection_note,
                },
                {
                    "type": "paragraph",
                    "text": "در زمان ثبت درخواست، شما دستگاه یا دستگاه‌ها را مشخص می‌کنید، مشکل‌های مشاهده‌شده را انتخاب می‌کنید و توضیح خودتان را می‌نویسید. این اطلاعات همراه با کد پیگیری ثبت می‌شود تا روند بررسی برای شما قابل پیگیری باشد و تیم تعمیر هم تصویر واضح‌تری از مشکل داشته باشد.",
                },
                {
                    "type": "table",
                    "headers": ["مرحله", "کاربر چه کاری انجام می‌دهد؟", "نتیجه"],
                    "rows": [
                        ["ثبت درخواست", "مدل و نشانه‌های مشکل را وارد می‌کند", "یک کد پیگیری دریافت می‌کند"],
                        ["بررسی اولیه", "دستگاه توسط تیم فنی بررسی می‌شود", "مسیر عیب‌یابی مشخص می‌شود"],
                        ["اعلام نتیجه", "بعد از بررسی، وضعیت و هزینه احتمالی اعلام می‌شود", "تصمیم نهایی با آگاهی بیشتر انجام می‌شود"],
                    ],
                },
                {
                    "type": "quote",
                    "text": "در تعمیرات گیمینگ، توضیح دقیق کاربر گاهی به اندازه خود تست فنی ارزش دارد؛ چون نشان می‌دهد مشکل دقیقاً در چه شرایطی خودش را نشان می‌دهد.",
                },
                {
                    "type": "faq",
                    "items": [
                        {
                            "question": "آیا مشکل بدون بررسی حضوری قابل تشخیص قطعی است؟",
                            "answer": "خیر، تشخیص دقیق و هزینه نهایی بعد از بررسی تخصصی اعلام می‌شود.",
                        },
                        {
                            "question": f"آیا {primary_keyword} همیشه به معنی خرابی قطعه است؟",
                            "answer": "نه همیشه. بعضی نشانه‌ها می‌تواند از تنظیمات، کابل، شرایط نگهداری یا استفاده طولانی باشد. برای تشخیص دقیق باید دستگاه بررسی شود.",
                        },
                        {
                            "question": "قبل از ثبت درخواست تعمیر چه اطلاعاتی بنویسم؟",
                            "answer": "مدل دستگاه، زمان شروع مشکل، شرایط تکرار آن و هر اتفاقی مثل ضربه، داغ شدن یا استفاده از کابل خاص را کوتاه و دقیق بنویسید.",
                        },
                        {
                            "question": "آیا هزینه تعمیر از قبل مشخص است؟",
                            "answer": "هزینه قطعی قبل از بررسی اعلام نمی‌شود. بعد از بررسی تخصصی، وضعیت دستگاه و هزینه احتمالی شفاف‌تر مشخص می‌شود.",
                        },
                        {
                            "question": "اگر چند دستگاه مشکل داشته باشند چه کنم؟",
                            "answer": "در فرم تعمیر چیتس گیم می‌توانید دستگاه‌ها را جداگانه تعریف کنید و برای هرکدام مشکل‌ها و توضیح جدا بنویسید.",
                        },
                    ],
                },
                {
                    "type": "cta",
                    "variant": "repair_request",
                    "title": "درخواست تعمیر را با خیال راحت ثبت کنید",
                    "body": "اگر نشانه‌های مشکل را دیده‌اید، ثبت درخواست تعمیر کمک می‌کند دستگاه با اطلاعات کامل‌تر بررسی شود.",
                    "label": "ثبت درخواست تعمیر",
                },
            ],
            "image_prompts": [
                {
                    "placement": "featured",
                    "prompt": f"تصویر حرفه‌ای برای مقاله {topic}: کنسول یا کنترلر پلی‌استیشن روی میز تعمیرات گیمینگ، نور آبی برند، فضای تمیز، بدون متن روی تصویر",
                    "alt": f"راهنمای {primary_keyword} در چیتس گیم",
                }
            ],
            "internal_link_suggestions": [
                {
                    "label": "ثبت درخواست تعمیر",
                    "url": "/Repair",
                    "reason": "کاربر بعد از خواندن مقاله ممکن است آماده ثبت درخواست باشد.",
                }
            ],
        }


class OpenAICompatibleBlogAiProvider(BlogAiProvider):
    name = "openai_compatible"

    def generate(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        api_key = getattr(settings, "BLOG_AI_API_KEY", "")
        if not api_key:
            raise BlogAiConfigurationError()

        payload = {
            "model": settings.BLOG_AI_MODEL,
            "messages": [
                {"role": "system", "content": build_blog_ai_system_prompt()},
                {"role": "user", "content": build_blog_ai_user_prompt(input_data)},
            ],
            "temperature": 0.55,
            "max_tokens": 4500,
            "response_format": {"type": "json_object"},
        }
        try:
            response = requests.post(
                settings.BLOG_AI_API_URL,
                json=payload,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                timeout=settings.BLOG_AI_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            data = response.json()
            content = data["choices"][0]["message"]["content"]
            return json.loads(content)
        except (requests.RequestException, KeyError, IndexError, TypeError, ValueError) as exc:
            raise BlogAiProviderError() from exc


def get_blog_ai_provider() -> BlogAiProvider:
    provider_name = getattr(settings, "BLOG_AI_PROVIDER", "openai_compatible")
    if provider_name == "mock":
        if not getattr(settings, "BLOG_AI_MOCK_ENABLED", False):
            raise BlogAiConfigurationError()
        return MockBlogAiProvider()
    if provider_name in ("openai", "openai_compatible"):
        return OpenAICompatibleBlogAiProvider()
    raise BlogAiConfigurationError()


def generate_blog_ai_draft(input_data: Dict[str, Any], user=None) -> Dict[str, Any]:
    user_id = getattr(user, "id", None)
    topic = input_data.get("topic")
    model = getattr(settings, "BLOG_AI_MODEL", "")
    configured_provider = getattr(settings, "BLOG_AI_PROVIDER", "openai_compatible")
    logger.info(
        "blog_ai_draft_request user_id=%s topic=%s provider=%s model=%s",
        user_id,
        topic,
        configured_provider,
        model,
    )
    try:
        provider = get_blog_ai_provider()
        raw_payload = provider.generate(input_data)
        draft = validate_blog_ai_draft_payload(raw_payload)
        logger.info("blog_ai_draft_success user_id=%s topic=%s provider=%s model=%s", user_id, topic, provider.name, model)
        return draft
    except BlogAiValidationError as exc:
        logger.warning(
            "blog_ai_draft_validation_failure user_id=%s topic=%s provider=%s errors=%s",
            user_id,
            topic,
            provider.name,
            exc.errors,
        )
        raise
    except BlogAiError:
        logger.warning("blog_ai_draft_failure user_id=%s topic=%s provider=%s", user_id, topic, configured_provider)
        raise
    except Exception as exc:
        logger.exception("blog_ai_draft_unexpected_failure user_id=%s topic=%s provider=%s", user_id, topic, configured_provider)
        raise BlogAiProviderError() from exc
