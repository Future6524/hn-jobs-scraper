from hn_jobs_scraper.parser import looks_like_job_post, parse_comment

# Comment bodies as the HN API returns them (HTML, escaped entities).
PIPE_HEADER = (
    "Acme Robotics | Senior Backend Engineer (Python) | Berlin, Germany or REMOTE (EU) | "
    "Full-time | €80k-€100k<p>We build warehouse robots. Stack: Python, FastAPI, "
    "PostgreSQL, Kubernetes on AWS.<p>Apply: "
    '<a href="https:&#x2F;&#x2F;jobs.lever.co&#x2F;acme&#x2F;123" rel="nofollow">'
    "https:&#x2F;&#x2F;jobs.lever.co&#x2F;acme&#x2F;123</a>"
)

HYBRID_WITH_EMAIL = (
    "Northwind Data | NYC (Hybrid, 3 days in office) | Data Engineer, Staff ML Engineer | "
    "$150k - $190k + equity<p>Raised a $20M Series A. We use Spark, dbt and Snowflake."
    "<p>Email hiring [at] northwind [dot] io with &quot;HN&quot; in the subject."
)

CONTRACT_NO_PIPES = (
    "Scrapeworks — Contract Web Scraping Developer — Remote (Worldwide)<p>"
    "Short-term contract, $60&#x2F;hr. Playwright + Python, some Node.js. "
    "No remote restrictions. Send a note to jobs@scrapeworks.dev"
)


def test_pipe_header_fields():
    post = parse_comment(PIPE_HEADER, comment_id=101, timestamp=1_780_000_000, keywords=["python", "rust"])

    assert post.company == "Acme Robotics"
    assert post.roles == "Senior Backend Engineer (Python)"
    assert post.location == "Berlin, Germany or REMOTE (EU)"
    assert post.remote == "yes"
    assert post.job_type == "Full-time"
    assert post.salary == "€80k-€100k"
    assert {"Python", "FastAPI", "PostgreSQL", "Kubernetes", "AWS"} <= set(post.tech_stack.split(", "))
    assert post.apply_link == "https://jobs.lever.co/acme/123"
    assert post.hn_url == "https://news.ycombinator.com/item?id=101"
    assert post.posted == "2026-05-28"
    assert post.match == "yes" and post.matched_keywords == "python"


def test_hybrid_salary_ignores_funding_and_deobfuscates_email():
    post = parse_comment(HYBRID_WITH_EMAIL, comment_id=202)

    assert post.company == "Northwind Data"
    assert post.remote == "hybrid"
    assert post.location.startswith("NYC")
    assert "Data Engineer" in post.roles
    assert post.salary.startswith("$150k - $190k")
    assert "$20M" not in post.salary
    assert post.apply_email == "hiring@northwind.io"
    assert {"Spark", "dbt", "Snowflake"} <= set(post.tech_stack.split(", "))


def test_dash_separated_contract_post_and_keyword_prefix_match():
    post = parse_comment(CONTRACT_NO_PIPES, comment_id=303, keywords=["scraping", "contract", "golang"])

    assert post.company == "Scrapeworks"
    assert post.roles == "Contract Web Scraping Developer"
    assert post.remote == "yes"
    assert post.salary == "$60/hr"
    assert post.apply_email == "jobs@scrapeworks.dev"
    assert {"Python", "Node.js", "Playwright", "Web Scraping"} <= set(post.tech_stack.split(", "))
    assert post.match == "yes"
    assert post.matched_keywords == "scraping, contract"
    assert looks_like_job_post(post)


def test_meta_comment_is_not_a_job_post():
    assert not looks_like_job_post(parse_comment("there used to be 800+ posts here :(", comment_id=404))
