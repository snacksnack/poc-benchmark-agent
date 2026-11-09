"""The page set, the navigation, the link rewriting and the page shell.

Ported from api-compatibility-lab's `publish` (its ADR 0008), minus the
copied-HTML path: every page here is markdown.

Links are the part that needs care. Each relative link in `docs/` is written
to resolve on disk and on GitHub (`tests/test_engagement.py` checks the
writeup's). On the web neither holds, so each link is resolved against its own
file, then pointed at the page that renders that document or sent to the
source on GitHub. A link that resolves to nothing fails the build rather than
becoming a 404 somebody finds later.
"""

import posixpath
import re
from dataclasses import dataclass
from html import escape
from pathlib import Path

from markdown_it import MarkdownIt

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
REPO = "https://github.com/snacksnack/poc-benchmark-agent"

RESULTS = "Results"
METHOD = "Method"
DECISIONS = "Decisions"
SECTIONS = (RESULTS, METHOD, DECISIONS)


@dataclass(frozen=True)
class Page:
    source: str   # relative to the repository root
    out: str      # relative to the site root
    nav: str
    section: str


def _adr_pages() -> list[Page]:
    """Every ADR, labeled from its filename rather than from a second list."""
    pages = []
    for path in sorted(DOCS.glob("adr/*.md")):
        number, _, rest = path.stem.partition("-")
        words = rest.replace("-", " ")
        pages.append(Page(f"docs/adr/{path.name}", f"adr/{path.stem}.html",
                          f"{number} {words[:1].upper()}{words[1:]}", DECISIONS))
    return pages


def pages() -> list[Page]:
    """The site in reading order. The writeup is the index: it states the
    question, the answer and the limits, and links everything else."""
    def md(name: str, nav: str, section: str) -> Page:
        return Page(f"docs/{name}.md", f"{name}.html", nav, section)

    return [
        Page("docs/engagement.md", "index.html", "Engagement writeup", RESULTS),
        md("results", "Results document", RESULTS),
        md("loop-log", "Loop log", RESULTS),
        md("criteria", "Success criteria", RESULTS),
        md("nastiness", "Nastiness list", METHOD),
        md("decisions", "Decisions", METHOD),
        md("risks", "Risk log", METHOD),
        md("cost-estimate", "Cost estimate", METHOD),
        md("noise-gate", "Noise gate", METHOD),
        md("phase1-profile", "Profile", METHOD),
        md("phase3-baselines", "Baselines", METHOD),
        *(md(f"phase{n}-findings", f"Phase {n} findings", METHOD) for n in (0, 1, 3, 4, 5, 6)),
        *_adr_pages(),
    ]


def routes(page_list: list[Page]) -> dict[str, str]:
    return {p.source: p.out for p in page_list}


def _github(rel: str) -> str:
    kind = "tree" if (ROOT / rel).is_dir() else "blob"
    return f"{REPO}/{kind}/main/{rel}"


def resolve(href: str, *, source: str, out: str, table: dict[str, str]) -> str:
    """Rewrite one link relative to where its page is published."""
    if re.match(r"^(?:[a-z][a-z0-9+.-]*:|//|#)", href):
        return href
    path, sep, fragment = href.partition("#")
    if not path:
        return href
    target = ((ROOT / source).parent / path).resolve()
    try:
        rel = target.relative_to(ROOT).as_posix()
    except ValueError:
        raise ValueError(f"{source}: link {href!r} points outside the repository") from None
    if not target.exists():
        raise ValueError(f"{source}: link {href!r} resolves to nothing")
    if rel in table:
        return posixpath.relpath(table[rel], posixpath.dirname(out) or ".") + sep + fragment
    return _github(rel) + sep + fragment


def _markdown(table: dict[str, str]) -> MarkdownIt:
    md = MarkdownIt("commonmark").enable(["table"])

    def link_open(self, tokens, idx, options, env):
        token = tokens[idx]
        href = token.attrGet("href")
        if href is not None:
            token.attrSet("href", resolve(href, source=env["source"], out=env["out"],
                                          table=table))
        return self.renderToken(tokens, idx, options, env)

    def table_open(self, tokens, idx, options, env):
        # Wide tables scroll inside their own box instead of the page.
        return '<div class="tw">' + self.renderToken(tokens, idx, options, env)

    def table_close(self, tokens, idx, options, env):
        return self.renderToken(tokens, idx, options, env) + "</div>"

    md.add_render_rule("link_open", link_open)
    md.add_render_rule("table_open", table_open)
    md.add_render_rule("table_close", table_close)
    return md


CSS = """
:root {
  color-scheme: light;
  --page: #f9f9f7; --surface: #fcfcfb;
  --ink: #0b0b0b; --ink-2: #52514e; --ink-muted: #898781;
  --hairline: #e1e0d9; --baseline: #c3c2b7;
  --link: #2a78d6; --border: rgba(11, 11, 11, 0.1);
}
@media (prefers-color-scheme: dark) {
  :root {
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19;
    --ink: #f0efec; --ink-2: #c3c2b7; --ink-muted: #898781;
    --hairline: #2c2c2a; --baseline: #383835;
    --link: #6aa9ec; --border: rgba(255, 255, 255, 0.1);
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--page); color: var(--ink);
  font: 15px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 920px; margin: 0 auto; padding: 32px 16px 64px; }
a { color: var(--link); text-decoration-thickness: 1px; text-underline-offset: 2px; }
a:hover { text-decoration-thickness: 2px; }
header.site { border-bottom: 1px solid var(--hairline); background: var(--surface); }
header.site .inner { max-width: 920px; margin: 0 auto; padding: 14px 16px; }
header.site .home { font-weight: 600; color: var(--ink); text-decoration: none; }
header.site .tag { color: var(--ink-2); font-size: 0.85rem; margin-left: 8px; }
nav.site { margin-top: 10px; display: flex; flex-direction: column; gap: 6px; }
nav.site section { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 10px; }
nav.site h2 { font-size: 0.72rem; margin: 0; color: var(--ink-muted);
  text-transform: uppercase; letter-spacing: 0.04em; min-width: 72px; }
nav.site a { font-size: 0.8rem; color: var(--ink-2); }
nav.site a[aria-current="page"] { color: var(--ink); font-weight: 600; text-decoration: none; }
nav.site details summary { font-size: 0.8rem; color: var(--ink-2); cursor: pointer; }
nav.site details[open] { display: flex; flex-wrap: wrap; gap: 4px 10px; }
article h1 { font-size: 1.5rem; margin: 0 0 16px; }
article h2 { font-size: 1.1rem; margin: 36px 0 10px; }
article h3 { font-size: 0.95rem; margin: 24px 0 6px; }
article p, article li { overflow-wrap: break-word; }
.tw { overflow-x: auto; margin: 16px 0; }
table { border-collapse: collapse; width: 100%; font-size: 0.85rem; }
th { text-align: left; color: var(--ink-2); font-weight: 600; }
th, td { padding: 6px 12px 6px 0; border-bottom: 1px solid var(--hairline); vertical-align: top; }
code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.85em;
  overflow-wrap: anywhere; }
blockquote { margin: 12px 0; padding: 2px 0 2px 14px;
  border-left: 2px solid var(--baseline); color: var(--ink-2); }
blockquote p { margin: 6px 0; }
pre { background: var(--surface); border: 1px solid var(--border);
  border-radius: 8px; padding: 12px 14px; overflow-x: auto; font-size: 0.82rem; }
pre code { font-size: 1em; }
footer.site { border-top: 1px solid var(--hairline); color: var(--ink-2); font-size: 0.82rem; }
footer.site .inner { max-width: 920px; margin: 0 auto; padding: 16px 16px 48px; }
"""

_H1 = re.compile(r"^#\s+(.+)$", re.MULTILINE)
CURRENT = ' aria-current="page"'


def nav(page_list: list[Page], current: Page) -> str:
    """Grouped navigation. Twenty-seven ADRs would bury the rest, so the
    Decisions group folds unless one of its pages is open."""
    here = posixpath.dirname(current.out) or "."
    blocks = []
    for section in SECTIONS:
        members = [p for p in page_list if p.section == section]
        links = "".join(
            f'<a href="{escape(posixpath.relpath(p.out, here))}"'
            f'{CURRENT if p.out == current.out else ""}>{escape(p.nav)}</a>'
            for p in members)
        if section == DECISIONS:
            opened = " open" if current.section == DECISIONS else ""
            links = (f"<details{opened}><summary>{len(members)} ADRs</summary>"
                     f"{links}</details>")
        blocks.append(f"<section><h2>{escape(section)}</h2>{links}</section>")
    return "".join(blocks)


def _page(title: str, chrome: str, body: str, tail: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>{title}</title>
<style>{CSS}</style>
</head>
<body>
{chrome}<main><article>{body}</article></main>{tail}
</body>
</html>
"""


def build(out_dir: Path, *, commit: str = "") -> list[Path]:
    page_list = pages()
    table = routes(page_list)
    md = _markdown(table)
    built = f" Built from <code>{escape(commit[:7])}</code>." if commit else ""
    tail = ('<footer class="site"><div class="inner">Generated from the committed documents '
            f'in <a href="{REPO}">snacksnack/poc-benchmark-agent</a>; no figure on these pages '
            f"was entered by hand.{built}</div></footer>")
    written = []
    for page in page_list:
        source = ROOT / page.source
        if not source.exists():
            raise ValueError(f"{page.source}: no such document")
        text = source.read_text()
        home = posixpath.relpath("index.html", posixpath.dirname(page.out) or ".")
        chrome = ('<header class="site"><div class="inner">'
                  f'<a class="home" href="{escape(home)}">POC Benchmark Agent</a>'
                  '<span class="tag">Neon Postgres sizing, run as a customer POC</span>'
                  f'<nav class="site" aria-label="Documents">{nav(page_list, page)}</nav>'
                  "</div></header>")
        found = _H1.search(text)
        title = found.group(1).strip() if found else page.nav
        body = md.render(text, {"source": page.source, "out": page.out})
        target = out_dir / page.out
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_page(escape(title), chrome, body, tail))
        written.append(target)
    (out_dir / ".nojekyll").write_text("")  # Pages runs Jekyll unless told not to
    return written
