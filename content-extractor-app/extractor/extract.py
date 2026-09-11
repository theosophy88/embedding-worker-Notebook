"""Turn a downloaded HTML page into clean article text.

trafilatura does the real work; the regex cleaner (ported from the original n8n
Code node) is the fallback for pages it cannot make sense of. Bot-challenge
pages are detected and reported, never "solved".
"""
from __future__ import annotations

import html as html_lib
import logging
import re

log = logging.getLogger(__name__)

try:
    import trafilatura
except ImportError:  # doctor reports this clearly
    trafilatura = None

# High-confidence markers that live in the raw HTML (inside <script>/<meta>),
# so they must be looked for before any stripping happens.
CHALLENGE_HTML_MARKERS = (
    "/cdn-cgi/challenge-platform", "cf-browser-verification", "__cf_chl",
    "cf_chl_opt", "_incapsula_resource", "distil_r_captcha", "px-captcha",
    "/captcha/", "g-recaptcha", "hcaptcha.com", "datadome", "perimeterx",
    "akamai bot manager",
)

# Phrases that appear in the visible text of challenge / block pages.
CHALLENGE_TEXT_PHRASES = (
    "client challenge", "javascript is disabled in your browser",
    "enable javascript and cookies to continue", "please enable javascript",
    "please turn on javascript", "javascript is required",
    "requires javascript to be enabled", "checking your browser before accessing",
    "verify you are human", "verifying you are human", "are you a robot",
    "i am not a robot", "complete the security check", "security check to access",
    "attention required cloudflare", "ddos protection by cloudflare",
    "access to this page has been denied",
    "access denied you don t have permission", "your request has been blocked",
    "unusual traffic from your computer network", "this site requires cookies",
    "ray id", "403 forbidden", "just a moment",
)

STRIP_TAGS = ("script", "style", "noscript", "nav", "footer", "header", "aside")

TAG_SPAM_RE = re.compile(r"([A-Z0-9_]+;){5,}")
TITLE_RE = re.compile(r"<title[^>]*>([\s\S]*?)</title>", re.I)
ARTICLE_RE = re.compile(r"<article[\s\S]*?</article>", re.I)
MAIN_RE = re.compile(r"<main[\s\S]*?</main>", re.I)
TAGS_RE = re.compile(r"<[^>]+>")
SPACES_RE = re.compile(r"\s{2,}")
NON_WORD_RE = re.compile(r"[^\w\s£$€¥¢₺₹₩₪&]", re.UNICODE)
TAILWIND_RE = re.compile(r"\[[^\]]*\]:\S+")
PSEUDO_CLASS_RE = re.compile(r":[a-zA-Z-]+\b")
ALNUM_RE = re.compile(r"[^a-z0-9]+")


class ExtractError(Exception):
    """The page cannot be used. `reason` is short and machine-readable."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def normalise(text: str) -> str:
    """Lowercase, entity-decoded, punctuation-free - for phrase matching only."""
    return ALNUM_RE.sub(" ", html_lib.unescape(text or "").lower()).strip()


def find_challenge_phrase(normalised_text: str) -> str | None:
    for phrase in CHALLENGE_TEXT_PHRASES:
        if phrase in normalised_text:
            return phrase
    return None


def detect_challenge(raw_html: str) -> str | None:
    """Return a marker if this page is a bot wall / interstitial, else None."""
    lowered = raw_html.lower()
    for marker in CHALLENGE_HTML_MARKERS:
        if marker in lowered:
            return marker

    title = TITLE_RE.search(raw_html)
    if title:
        hit = find_challenge_phrase(normalise(title.group(1)))
        if hit:
            return f"title:{hit}"
    return None


def is_tag_spam(text: str) -> bool:
    """Pages that dump 'TAG1;TAG2;TAG3;...' instead of an article body."""
    return bool(TAG_SPAM_RE.search((text or "").strip()))


def regex_extract(raw_html: str) -> str:
    """Fallback extractor - the logic the original n8n Code node used."""
    cleaned = raw_html
    for tag in STRIP_TAGS:
        cleaned = re.sub(rf"<{tag}[\s\S]*?</{tag}>", " ", cleaned, flags=re.I)

    def strip_tags(fragment: str) -> str:
        return SPACES_RE.sub(" ", TAGS_RE.sub(" ", fragment or "")).strip()

    article = ARTICLE_RE.search(cleaned)
    main = MAIN_RE.search(cleaned)

    article_text = strip_tags(article.group(0) if article else "")
    main_text = strip_tags(main.group(0) if main else "")
    if is_tag_spam(article_text):
        article_text = ""
    if is_tag_spam(main_text):
        main_text = ""

    if len(article_text) > 500:
        return article_text
    if len(main_text) > 500:
        return main_text

    full_text = strip_tags(cleaned)
    if is_tag_spam(full_text):
        raise ExtractError("tag_spam")
    return full_text


class Extractor:
    """Extraction bound to a Settings object, so the panel can retune it live."""

    def __init__(self, settings) -> None:
        self.settings = settings

    def clean(self, text: str) -> str:
        text = html_lib.unescape(text or "")

        # Tailwind/CSS leftovers that survive tag stripping: "[&>*]:mt-4", ":hover"
        text = TAILWIND_RE.sub(" ", text)
        text = PSEUDO_CLASS_RE.sub(" ", text)

        if self.settings.strip_punctuation:
            # Keep letters and digits of ANY script (Persian, Arabic, Cyrillic
            # all survive), whitespace, currency symbols and "&".
            text = NON_WORD_RE.sub(" ", text)

        text = SPACES_RE.sub(" ", text).strip()
        return text[: self.settings.max_content_chars]

    def extract(self, raw_html: str, url: str) -> tuple[str, str]:
        """Return (clean_text, method). Raises ExtractError if unusable."""
        marker = detect_challenge(raw_html)
        if marker:
            raise ExtractError(f"bot_challenge:{marker}")

        text: str | None = None
        method = "regex"

        if trafilatura is not None:
            try:
                text = trafilatura.extract(
                    raw_html, url=url,
                    include_comments=False, include_tables=False,
                    favor_precision=True, no_fallback=False,
                )
                if text:
                    method = "trafilatura"
            except Exception as exc:  # malformed markup, recursion limits, ...
                log.debug("trafilatura failed on %s: %s", url, exc)
                text = None

        if not text or len(text) < self.settings.min_content_chars:
            text, method = regex_extract(raw_html), "regex"

        # Challenge pages are short. A real article that merely *mentions* one
        # of the phrases will be long, so only reject when the page is thin too.
        normalised = normalise(text)
        hit = find_challenge_phrase(normalised)
        if hit and len(normalised) < 1500:
            raise ExtractError(f"bot_challenge:text:{hit}")

        content = self.clean(text)
        if len(content) < self.settings.min_content_chars:
            raise ExtractError(f"too_short:{len(content)}")

        return content, method
