"""Heuristic parser for "Who is hiring?" comments.

Most posts open with a pipe-separated header such as::

    Acme Corp | Senior Backend Engineer | Berlin or REMOTE (EU) | Full-time | €80k-€100k

The parser splits that header into segments, classifies each one (role,
location, salary, job type) and then mines the full comment body for
anything the header did not cover: remote policy, salary, tech stack,
apply links and emails.
"""

from __future__ import annotations

import html
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

HN_ITEM_URL = "https://news.ycombinator.com/item?id={id}"

# --------------------------------------------------------------------------- #
# HTML -> text
# --------------------------------------------------------------------------- #

_HREF_RE = re.compile(r'<a\s[^>]*href="([^"]+)"', re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_URL_RE = re.compile(r"https?://[^\s<>()|,]+", re.I)


def html_to_text(raw: str) -> tuple[str, list[str]]:
    """Convert HN comment HTML to plain text and return it with all link targets."""
    links = [html.unescape(h) for h in _HREF_RE.findall(raw or "")]
    text = re.sub(r"<p>", "\n\n", raw or "", flags=re.I)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    # Bare URLs in the text that were not anchors.
    for url in _URL_RE.findall(text):
        url = url.rstrip(".;:!?")
        if url not in links:
            links.append(url)
    return text, links


# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #

ROLE_WORDS = re.compile(
    r"\b(engineer\w*|developer\w*|dev\b|programmer|designer|architect|scientist|"
    r"researcher|analyst|manager|lead\b|head of|director|vp\b|cto|cfo|coo|"
    r"founding|intern\w*|sre|devops|swe|marketer|marketing|sales|recruiter|"
    r"operations|ops\b|support|writer|consultant|specialist|administrator|"
    r"frontend|front-end|backend|back-end|full[- ]?stack|fullstack|"
    r"qa\b|technician|account executive|customer success|"
    r"mts\b|member of technical staff|roles?\b|positions?\b|openings?\b)",
    re.I,
)
# Stricter vocabulary for picking role titles out of the comment body.
BODY_ROLE_RE = re.compile(
    r"\b(engineer|developer|designer|scientist|researcher|architect|analyst|"
    r"manager|director|head of|intern|sre|devops|recruiter|account executive)s?\b",
    re.I,
)
NON_JOB_HINT_RE = re.compile(
    r"apply|hiring|career|resume|\bcv\b|position|role|salary|remote|onsite|"
    r"full[- ]?time|e-?mail|reach out|send", re.I
)
MAX_SEGMENT_LEN = 90  # longer header segments are prose, not fields

LOCATION_WORDS = re.compile(
    r"\b(remote\w*|on[- ]?site|onsite|in[- ]office|office|hybrid|anywhere|"
    r"worldwide|global|usa?|u\.s\.?|uk|eu|emea|apac|latam|europe|americas|"
    r"north america|canada|germany|france|india|australia|netherlands|"
    r"spain|sweden|switzerland|ireland|israel|japan|singapore|brazil|"
    r"mexico|portugal|poland|"
    r"sf|nyc|bay area|san francisco|new york|los angeles|seattle|boston|"
    r"austin|chicago|denver|portland|atlanta|miami|toronto|vancouver|"
    r"montreal|london|berlin|munich|amsterdam|paris|dublin|stockholm|"
    r"zurich|barcelona|madrid|lisbon|tel aviv|tokyo|sydney|bangalore|"
    r"timezone|time zone|(?-i:[A-Z]{1,3}T)\b|utc|gmt)",
    re.I,
)
# "City, ST" / "City, Country"
CITY_COMMA_RE = re.compile(r"^[A-Z][a-zA-Z .'-]+,\s*[A-Z][a-zA-Z .]+")

JOB_TYPE_RE = re.compile(
    r"\b(full[- ]?time|part[- ]?time|contract(?:or)?|freelance|internship|"
    r"intern|permanent|temp(?:orary)?|fte|c2h|contract[- ]to[- ]hire)\b",
    re.I,
)

_CURRENCY = r"(?:[$€£¥]|USD|EUR|GBP|CAD|AUD|CHF|SEK|NOK|DKK|INR)"
_AMOUNT = r"\d[\d,.]*\s?[kK]?"
_UNIT = r"(?:\s?(?:/|per\s|an?\s)\s?(?:hr|hour|h|year|yr|annum|annually|month|mo|day|week))"
SALARY_RE = re.compile(
    rf"{_CURRENCY}\s?{_AMOUNT}"
    rf"(?:\s?(?:-|–|—|to)\s?{_CURRENCY}?\s?{_AMOUNT})?"
    rf"(?:\s?(?:USD|EUR|GBP|CAD|AUD|CHF))?"
    rf"{_UNIT}?"
    rf"|\b\d{{2,3}}\s?[kK]?\s?(?:-|–|—|to)\s?\d{{2,3}}\s?[kK]\b{_UNIT}?"
    rf"(?:\s?(?:USD|EUR|GBP|CAD|AUD|CHF))?",
)
_FUNDING_TAIL = re.compile(r"^[\d,.]*\s?(?:[mMbB]\b|mm\b|million|billion|bn\b)", re.I)

# Display name -> pattern. Case-insensitive unless the pattern sets its own flags.
TECH_KEYWORDS: dict[str, str] = {
    "Python": r"\bpython\b",
    "JavaScript": r"\bjavascript\b|\bjs\b",
    "TypeScript": r"\btypescript\b|\bts\b",
    "Node.js": r"\bnode(?:\.?js)?\b",
    "React": r"\breact(?:\.?js)?\b(?!\s+native)",
    "React Native": r"\breact\s+native\b",
    "Vue": r"\bvue(?:\.?js)?\b",
    "Angular": r"\bangular\b",
    "Svelte": r"\bsvelte(?:kit)?\b",
    "Next.js": r"\bnext\.?js\b",
    "Django": r"\bdjango\b",
    "Flask": r"\bflask\b",
    "FastAPI": r"\bfastapi\b",
    "Ruby": r"\bruby\b",
    "Rails": r"\brails\b",
    "PHP": r"\bphp\b",
    "Laravel": r"\blaravel\b",
    "Go": r"\bgolang\b|(?-i:(?<![\w.])Go(?=\s*[,/)|;]|\s+(?:and|or)\b))",
    "Rust": r"\brust\b",
    "Java": r"\bjava\b(?!\s*script)",
    "Kotlin": r"\bkotlin\b",
    "Scala": r"\bscala\b",
    "Swift": r"\bswift(?:ui)?\b",
    "C++": r"(?<!\w)c\+\+(?!\w)",
    "C#": r"(?<!\w)c#(?!\w)",
    ".NET": r"(?<!\w)\.net\b",
    "Elixir": r"\belixir\b",
    "Erlang": r"\berlang\b",
    "Haskell": r"\bhaskell\b",
    "Clojure": r"\bclojure(?:script)?\b",
    "OCaml": r"\bocaml\b",
    "Zig": r"\bzig\b",
    "Flutter": r"\bflutter\b",
    "SQL": r"\bsql\b",
    "PostgreSQL": r"\bpostgres(?:ql)?\b",
    "MySQL": r"\bmysql\b",
    "MongoDB": r"\bmongo(?:db)?\b",
    "Redis": r"\bredis\b",
    "Kafka": r"\bkafka\b",
    "Spark": r"\b(?:apache\s+)?spark\b",
    "Snowflake": r"\bsnowflake\b",
    "dbt": r"\bdbt\b",
    "GraphQL": r"\bgraphql\b",
    "AWS": r"\baws\b",
    "GCP": r"\bgcp\b|\bgoogle cloud\b",
    "Azure": r"\bazure\b",
    "Kubernetes": r"\bkubernetes\b|\bk8s\b",
    "Docker": r"\bdocker\b",
    "Terraform": r"\bterraform\b",
    "Linux": r"\blinux\b",
    "iOS": r"\bios\b",
    "Android": r"\bandroid\b",
    "LLM": r"\bllms?\b",
    "PyTorch": r"\bpytorch\b",
    "TensorFlow": r"\btensorflow\b",
    "CUDA": r"\bcuda\b",
    "Machine Learning": r"\bmachine learning\b|\bml\b",
    "Web Scraping": r"\bscrap(?:ing|er|ers)\b|\bcrawl(?:ing|er|ers)\b",
    "Playwright": r"\bplaywright\b",
    "Selenium": r"\bselenium\b",
}
_TECH_RES = {name: re.compile(p, re.I) for name, p in TECH_KEYWORDS.items()}

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_OBFUSCATED_AT = re.compile(r"\s*[\[({<]\s*at\s*[\])}>]\s*|\s+\(at\)\s+", re.I)
_OBFUSCATED_DOT = re.compile(r"\s*[\[({<]\s*dot\s*[\])}>]\s*", re.I)
APPLY_HINT_RE = re.compile(
    r"apply|career|jobs?\b|hiring|join|greenhouse|lever\.co|ashbyhq|workable|"
    r"recruitee|bamboohr|wellfound|angel\.co|breezy|smartrecruiters|teamtailor|"
    r"workatastartup|personio|rippling|notion\.site|forms?\.",
    re.I,
)

# --------------------------------------------------------------------------- #
# Model
# --------------------------------------------------------------------------- #


@dataclass
class JobPost:
    id: int
    company: str = ""
    roles: str = ""
    location: str = ""
    remote: str = "no"  # yes / no / hybrid
    job_type: str = ""
    salary: str = ""
    tech_stack: str = ""
    apply_link: str = ""
    apply_email: str = ""
    posted: str = ""
    hn_url: str = ""
    match: str = ""
    matched_keywords: str = ""
    description: str = ""
    text: str = field(default="", repr=False)
    header_segments: int = field(default=0, repr=False)

    def to_row(self) -> dict[str, str | int]:
        row = asdict(self)
        for key in _INTERNAL_FIELDS:
            row.pop(key)
        return row


_INTERNAL_FIELDS = ("text", "header_segments")
EXPORT_COLUMNS = [k for k in JobPost.__dataclass_fields__ if k not in _INTERNAL_FIELDS]

# --------------------------------------------------------------------------- #
# Field extractors
# --------------------------------------------------------------------------- #


def split_header(first_line: str) -> list[str]:
    parts = [p.strip(" \t-–—•") for p in re.split(r"\s*\|\s*", first_line)]
    return [p for p in parts if p]


def _strip_urls(value: str) -> str:
    value = _URL_RE.sub("", value)
    value = re.sub(r"\(\s*\)", "", value)
    return re.sub(r"\s{2,}", " ", value).strip(" ,;:-")


def classify_remote(header: str, body: str) -> str:
    """Return 'yes', 'no' or 'hybrid' using the header first, then the body."""
    negated = re.compile(
        r"\b(?:no|not|non)[- ]remote\b|\bremote\s*:\s*no\b|\bremote\s+not\s+(?:possible|available)\b|"
        r"\b(?:on[- ]?site|in[- ]office|in[- ]person)\s+only\b",
        re.I,
    )
    remote = re.compile(r"\bremote\b|\bwfh\b|\bwork from (?:home|anywhere)\b|\bdistributed\b", re.I)
    hybrid = re.compile(r"\bhybrid\b", re.I)
    fully = re.compile(
        r"\b(?:fully|100%|remote[- ]first|remote[- ]friendly|remote ok|or remote|remote or)\b|"
        r"100%\s+remote|remote[- ](?:first|friendly|ok)",
        re.I,
    )

    for text in (header, body):
        if not text:
            continue
        if negated.search(text) and not hybrid.search(text):
            return "no"
        has_remote = bool(remote.search(text))
        has_hybrid = bool(hybrid.search(text))
        if has_remote and has_hybrid:
            return "yes" if fully.search(text) else "hybrid"
        if has_hybrid:
            return "hybrid"
        if has_remote:
            return "yes"
        if text is header and re.search(r"\b(?:on[- ]?site|in[- ]office)\b", text, re.I):
            return "no"
    return "no"


def extract_salary(text: str) -> str:
    found: list[str] = []
    for m in SALARY_RE.finditer(text):
        tail = text[m.end() : m.end() + 12]
        if _FUNDING_TAIL.match(tail):
            continue  # "$20M Series A" is funding, not pay
        value = m.group(0).strip()
        digits = re.sub(r"[^\d]", "", value.split("-")[0].split("to")[0])
        has_k = bool(re.search(r"\d\s?[kK]", value))
        has_unit = bool(re.search(_UNIT, value))
        if not has_k and not has_unit and digits and int(digits[:7]) < 1000:
            continue  # "$5 off", "$99/mo product" etc.
        if value not in found:
            found.append(value)
        if len(found) == 2:
            break
    return "; ".join(found)


def extract_tech(text: str) -> list[str]:
    return [name for name, rx in _TECH_RES.items() if rx.search(text)]


def extract_emails(text: str) -> list[str]:
    deobfuscated = _OBFUSCATED_DOT.sub(".", _OBFUSCATED_AT.sub("@", text))
    emails: list[str] = []
    for e in EMAIL_RE.findall(deobfuscated):
        e = e.rstrip(".")
        if e.lower() not in (x.lower() for x in emails):
            emails.append(e)
    return emails


def pick_apply_link(links: list[str]) -> str:
    links = [l for l in links if l.startswith("http") and "news.ycombinator.com" not in l]
    for link in links:
        if APPLY_HINT_RE.search(link):
            return link
    return links[0] if links else ""


def extract_body_roles(body: str, limit: int = 3) -> list[str]:
    """Pick short title-like lines ("Senior Backend Engineer", "- Product Designer") from the body."""
    roles: list[str] = []
    for line in body.splitlines():
        line = _strip_urls(line.strip().lstrip("-*•·–— ").strip())
        line = re.split(r"\s+[-–—:]\s+|:\s*$", line)[0].strip()
        if not (4 <= len(line) <= 70) or line.endswith((".", "?", "!")):
            continue
        if BODY_ROLE_RE.search(line) and line not in roles:
            roles.append(line)
            if len(roles) == limit:
                break
    return roles


def looks_like_job_post(post: JobPost) -> bool:
    """Filter out meta comments ("there used to be 800+ posts here") and replies."""
    if post.header_segments >= 2:
        return True
    if re.match(r"(?i)\s*(i am|i'm|i’m|i've|so people|there used to)\b", post.text):
        return False  # job seekers and meta chatter
    if re.search(r"(?i)\bhiring\b|careers", post.text) and re.search(r"https?://|\w\.\w+/", post.text):
        return True  # terse one-liners: "Hiring for multiple roles - https://..."
    return len(post.text) > 150 and bool(NON_JOB_HINT_RE.search(post.text))


def keyword_matches(text: str, keywords: list[str]) -> list[str]:
    """Case-insensitive prefix match so 'contract' also hits 'contractor'."""
    hits = []
    for kw in keywords:
        if kw and re.search(rf"(?<!\w){re.escape(kw)}", text, re.I):
            hits.append(kw)
    return hits


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def parse_comment(
    comment_html: str,
    comment_id: int = 0,
    timestamp: int | None = None,
    keywords: list[str] | None = None,
) -> JobPost:
    """Parse one top-level "Who is hiring?" comment into a JobPost."""
    text, links = html_to_text(comment_html)
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    header = lines[0] if lines else ""
    body = "\n".join(lines[1:])

    post = JobPost(id=comment_id, text=text)
    post.hn_url = HN_ITEM_URL.format(id=comment_id) if comment_id else ""
    if timestamp:
        post.posted = datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%d")

    header = re.sub(r"(?i)^\s*(company|employer)\s*:\s*", "", header)
    segments = split_header(header)
    if len(segments) <= 1 and header:
        # Some posters use " - " or " — " instead of pipes.
        alt = [s.strip() for s in re.split(r"\s+[-–—•]\s+", header) if s.strip()]
        if len(alt) > 1:
            segments = alt
    post.header_segments = len(segments)

    roles: list[str] = []
    locations: list[str] = []
    job_types: list[str] = []
    salaries: list[str] = []
    leftovers: list[str] = []

    if segments:
        company = _strip_urls(segments[0]) or segments[0]
        # "Flowstate (London, UK)" -> company "Flowstate", location "London, UK"
        paren = re.match(r"^(.*?)\s*\(([^)]*)\)\s*$", company)
        if paren and paren.group(1) and LOCATION_WORDS.search(paren.group(2)):
            company = paren.group(1).strip()
            locations.append(paren.group(2).strip())
        if len(company) > 60:
            company = company[:60].rsplit(" ", 1)[0] + "…"
        post.company = company
    for seg in segments[1:]:
        clean = _strip_urls(seg).lstrip("√✓✔*• ").strip()
        if not clean or len(clean) > MAX_SEGMENT_LEN:
            continue
        if SALARY_RE.search(clean) and extract_salary(clean) and not ROLE_WORDS.search(clean):
            salaries.append(extract_salary(clean))
            continue
        if len(extract_tech(clean)) >= 2 and not ROLE_WORDS.search(clean):
            continue  # "TypeScript / Postgres / React" is stack, picked up below
        is_role = bool(ROLE_WORDS.search(clean))
        is_loc = bool(LOCATION_WORDS.search(clean)) or bool(CITY_COMMA_RE.match(clean))
        is_type = bool(JOB_TYPE_RE.fullmatch(clean.strip(" ()"))) or (
            bool(JOB_TYPE_RE.search(clean)) and len(clean) <= 30 and not is_role
        )
        if is_type and not is_loc:
            job_types.append(clean)
        elif is_role and not (is_loc and re.match(r"(?i)\s*(remote|on[- ]?site|hybrid)\b", clean)):
            roles.append(clean)
        elif is_loc:
            locations.append(clean)
        else:
            leftovers.append(clean)

    # Many posts list roles in the body rather than the header.
    if not roles:
        roles = extract_body_roles(body)
    if not roles and leftovers and len(leftovers[0]) <= 60:
        roles.append(leftovers.pop(0))

    post.roles = "; ".join(roles)
    post.location = "; ".join(locations)
    post.remote = classify_remote(header, body)
    if not post.location and post.remote == "yes":
        post.location = "Remote"

    if not job_types:
        job_types = sorted({m.group(0).lower() for m in JOB_TYPE_RE.finditer(header)})
    post.job_type = ", ".join(job_types)

    post.salary = "; ".join(salaries) or extract_salary(header) or extract_salary(body)
    post.tech_stack = ", ".join(extract_tech(text))

    post.apply_link = pick_apply_link(links)
    emails = extract_emails(text)
    post.apply_email = emails[0] if emails else ""

    desc = re.sub(r"\s+", " ", body).strip()
    post.description = desc[:497] + "..." if len(desc) > 500 else desc

    if keywords:
        hits = keyword_matches(text, keywords)
        post.matched_keywords = ", ".join(hits)
        post.match = "yes" if hits else "no"

    return post
