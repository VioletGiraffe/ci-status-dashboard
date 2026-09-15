#!/usr/bin/env python3
"""Build a static page showing the GitHub Actions status of the default branch of every repository owned by the token's owner.

Configuration comes from environment variables, so the script runs the same inside the composite action and locally:
  DASHBOARD_TOKEN     fine-grained PAT with read-only Actions access to all repositories (required)
  ANONYMIZE_PRIVATE   anything other than "false" hides private repository names and details (so unset or empty means on)
  ANONYMIZE_SALT      HMAC key for private repository pseudonyms (required only when private repositories get anonymized)
  OUTPUT_DIR          directory index.html is written to (default: _site)
  STALE_AFTER_HOURS   the page warns when it is older than this; 0 disables the warning (default: 48)
"""

import datetime as dt
import hashlib
import hmac
import html
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API_URL = os.environ.get("GITHUB_API_URL", "https://api.github.com")
SERVER_URL = os.environ.get("GITHUB_SERVER_URL", "https://github.com")

STATES = ("failing", "pending", "passing")
PASSED_CONCLUSIONS = {"success", "neutral"}
# Skipped and cancelled runs say nothing about whether the code works. Judging by them would let e.g. a cancelled rerun hide an earlier failure.
IGNORED_CONCLUSIONS = {"skipped", "cancelled", "stale"}


class ApiError(Exception):
    pass


def fail(message):
    print(f"::error::{message}", file=sys.stderr)
    sys.exit(1)


def api_get(token, url, params=None):
    if not url.startswith("http"):
        url = API_URL + url
    if params:
        url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "ci-dashboard",
    })
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response), response.headers.get("Link", "")
        except urllib.error.HTTPError as error:
            if error.code in (502, 503, 504) and attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            try:
                detail = json.load(error).get("message", "")
            except (ValueError, AttributeError):
                detail = ""
            raise ApiError(f"HTTP {error.code} {error.reason}" + (f": {detail}" if detail else "")) from None


def next_page_url(link_header):
    for part in link_header.split(","):
        pieces = part.split(";")
        if len(pieces) >= 2 and pieces[1].strip() == 'rel="next"':
            return pieces[0].strip()[1:-1]
    return None


def api_get_all(token, path, params, key=None):
    items = []
    url = path
    while url:
        data, link = api_get(token, url, params)
        items.extend(data[key] if key else data)
        # The next-page URL already carries the query string.
        url, params = next_page_url(link), None
    return items


def pseudonym(salt, full_name):
    digest = hmac.new(salt.encode(), full_name.encode(), hashlib.sha256).hexdigest()
    return f"private-{digest[:6]}"


def latest_meaningful_run(runs):
    for run in runs:  # the API returns newest first
        # A pull request whose head branch shares the default branch's name (typically a fork's main) reports that name as head_branch.
        if run["event"].startswith("pull_request"):
            continue
        if run["status"] == "completed" and run["conclusion"] in IGNORED_CONCLUSIONS:
            continue
        return run
    return None


def run_state(run):
    if run["status"] != "completed" or run["conclusion"] == "action_required":
        return "pending"
    return "passing" if run["conclusion"] in PASSED_CONCLUSIONS else "failing"


def check_repo(token, repo):
    """Return the state of the latest meaningful default-branch run of each active workflow; empty if there are none."""
    base = f"/repos/{repo['full_name']}/actions"
    workflows = api_get_all(token, f"{base}/workflows", {"per_page": 100}, key="workflows")
    results = []
    for workflow in workflows:
        # Deleted and disabled workflows keep their last run forever, which would pin the repository to a stale state.
        if workflow["state"] != "active":
            continue
        runs, _ = api_get(token, f"{base}/workflows/{workflow['id']}/runs", {"branch": repo["default_branch"], "per_page": 20})
        run = latest_meaningful_run(runs["workflow_runs"])
        if run:
            results.append({"name": workflow["name"], "state": run_state(run), "url": run["html_url"], "time": run["updated_at"]})
    return results


def summarize(repo, label, hidden, workflows):
    state = next(s for s in STATES if any(w["state"] == s for w in workflows))
    relevant = [w for w in workflows if w["state"] == state]
    return {
        "label": label,
        "url": None if hidden else repo["html_url"],
        "hidden": hidden,
        "state": state,
        "workflows": [] if hidden else relevant,
        "count": len(workflows),
        "time": max(w["time"] for w in relevant),
    }


def plural(count, word, plural_word=None):
    return f"{count} {word if count == 1 else (plural_word or word + 's')}"


def time_html(iso):
    moment = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return f'<time datetime="{html.escape(iso)}">{moment.strftime("%Y-%m-%d %H:%M UTC")}</time>'


def row_html(row):
    esc = html.escape
    name = f'<a href="{esc(row["url"])}">{esc(row["label"])}</a>' if row["url"] else esc(row["label"])
    if row["hidden"]:
        workflows = '<span class="muted">Hidden for private repository</span>'
    elif row["state"] == "passing":
        workflows = f'<span class="muted">{plural(row["count"], "workflow")}</span>'
    else:
        workflows = ", ".join(f'<a href="{esc(w["url"])}">{esc(w["name"])}</a>' for w in row["workflows"])
    return f'<tr><th scope="row">{name}</th><td>{workflows}</td><td class="when">{time_html(row["time"])}</td></tr>'


def table_html(rows):
    body = "\n".join(row_html(r) for r in rows)
    return ('<table><thead><tr><th scope="col">Repository</th><th scope="col">Workflows</th><th scope="col" class="when">Last run</th></tr></thead>'
            f"<tbody>\n{body}\n</tbody></table>")


PAGE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>{title}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Atkinson+Hyperlegible:wght@400;700&display=swap">
<style>
:root {{
  color-scheme: light dark;
  --bg: #eef1f4; --ink: #1b2430; --muted: #5f6b7a; --rule: #d3d9e0;
  --fail: #c22e2e; --pend: #a86b0c; --pass: #3f8f5e; --pass-cell: #9fccae;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --bg: #171c23; --ink: #e3e8ee; --muted: #97a3b1; --rule: #2c343e;
    --fail: #f0625c; --pend: #e0a43a; --pass: #5dbb84; --pass-cell: #2f5a40;
  }}
}}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--bg); color: var(--ink); font: 1rem/1.5 "Atkinson Hyperlegible", system-ui, sans-serif; }}
main {{ max-width: 60rem; margin: 0 auto; padding: 3rem 1.25rem 4rem; }}
h1 {{ font-size: clamp(1.75rem, 5vw, 2.75rem); line-height: 1.1; letter-spacing: -0.01em; margin: 0 0 0.5rem; }}
.meta {{ margin: 0; color: var(--muted); }}
.stale {{ margin: 1rem 0 0; padding: 0.5rem 0 0.5rem 1rem; border-left: 3px solid var(--pend); }}
.wall {{ display: flex; flex-wrap: wrap; gap: 4px; margin: 2rem 0 0; }}
.cell {{ width: 14px; height: 14px; border-radius: 3px; }}
.cell.failing {{ background: var(--fail); }}
.cell.pending {{ box-shadow: inset 0 0 0 2px var(--pend); }}
.cell.passing {{ background: var(--pass-cell); }}
.group {{ margin-top: 2.5rem; }}
h2 {{ font-size: 1.125rem; margin: 0 0 0.5rem; }}
.glyph {{ display: inline-block; width: 1.25em; }}
.failing .glyph {{ color: var(--fail); }}
.pending .glyph {{ color: var(--pend); }}
.passing .glyph {{ color: var(--pass); }}
summary {{ cursor: pointer; }}
summary h2 {{ display: inline; }}
details[open] summary {{ margin-bottom: 0.5rem; }}
table {{ width: 100%; border-collapse: collapse; }}
th, td {{ text-align: left; vertical-align: top; font-weight: 400; padding: 0.6rem 1rem 0.6rem 0; border-bottom: 1px solid var(--rule); }}
thead th {{ color: var(--muted); font-size: 0.875rem; padding-top: 0.25rem; padding-bottom: 0.25rem; }}
tbody th {{ font-weight: 700; overflow-wrap: anywhere; }}
.when {{ text-align: right; white-space: nowrap; font-variant-numeric: tabular-nums; color: var(--muted); padding-right: 0; }}
.failing td a {{ color: var(--fail); }}
a {{ color: inherit; text-decoration-color: var(--muted); text-underline-offset: 0.2em; }}
a:hover {{ text-decoration-color: currentColor; }}
:focus-visible {{ outline: 2px solid var(--ink); outline-offset: 2px; }}
.muted {{ color: var(--muted); }}
.empty {{ margin-top: 2rem; max-width: 40rem; }}
</style>
</head>
<body>
<main>
<header>
<h1>{headline}</h1>
<p class="meta">{meta}</p>
<p class="stale" hidden></p>
</header>
{body}
</main>
<script>
const rtf = new Intl.RelativeTimeFormat("en", {{ numeric: "auto" }});
const units = [["year", 31536000], ["month", 2592000], ["day", 86400], ["hour", 3600], ["minute", 60]];
function ago(date) {{
  const seconds = (date - Date.now()) / 1000;
  for (const [unit, size] of units) if (Math.abs(seconds) >= size) return rtf.format(Math.round(seconds / size), unit);
  return "just now";
}}
for (const t of document.querySelectorAll("time[datetime]")) {{
  t.title = t.textContent;
  t.textContent = ago(new Date(t.dateTime));
}}
const generated = document.querySelector("time[data-generated]");
const staleHours = Number(generated.dataset.staleHours);
const ageHours = (Date.now() - new Date(generated.dateTime)) / 3600000;
if (staleHours > 0 && ageHours > staleHours) {{
  const note = document.querySelector(".stale");
  const [count, unit] = ageHours >= 48 ? [Math.floor(ageHours / 24), "day"] : [Math.floor(ageHours), "hour"];
  note.textContent = "This page hasn't been refreshed for " + count + " " + unit + (count === 1 ? "" : "s") + ", so the scheduled run may have stopped. " +
    "GitHub disables schedules in public repositories after 60 days without activity; re-enable the workflow from its Actions tab.";
  note.hidden = false;
}}
</script>
</body>
</html>
"""


def render(login, rows, generated, stale_hours):
    esc = html.escape
    groups = {s: sorted((r for r in rows if r["state"] == s), key=lambda r: r["time"], reverse=True) for s in STATES}
    failing, pending, passing = (len(groups[s]) for s in STATES)

    if not rows:
        headline = "No workflow runs found"
    elif failing:
        headline = f"{plural(failing, 'repository', 'repositories')} failing"
    elif pending:
        headline = f"Nothing failing, {pending} still running"
    else:
        headline = "Everything passes"

    updated = (f'<time datetime="{generated}" data-generated data-stale-hours="{stale_hours}">'
               f'{dt.datetime.fromisoformat(generated.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M UTC")}</time>')
    owner = f'<a href="{esc(SERVER_URL)}/{esc(login)}">{esc(login)}</a>'
    meta = f"GitHub Actions on the default branch of {plural(len(rows), 'repository', 'repositories')} owned by {owner}. Updated {updated}."

    if not rows:
        body = ('<p class="empty">None of the non-archived, non-fork repositories this token can see has an active workflow '
                "with a completed or running default-branch run.</p>")
    else:
        cells = "".join(f'<span class="cell {s}" title="{esc(r["label"])}"></span>' for s in STATES for r in groups[s])
        label = f"{failing} failing, {pending} running or waiting, {passing} passing"
        parts = [f'<div class="wall" role="img" aria-label="{label}">{cells}</div>']
        if failing:
            parts.append(f'<section class="group failing"><h2><span class="glyph" aria-hidden="true">✕</span>Failing</h2>{table_html(groups["failing"])}</section>')
        if pending:
            parts.append(f'<section class="group pending"><h2><span class="glyph" aria-hidden="true">◌</span>Running or waiting</h2>'
                         f'{table_html(groups["pending"])}</section>')
        if passing:
            parts.append(f'<details class="group passing"><summary><h2><span class="glyph" aria-hidden="true">✓</span>{passing} passing</h2></summary>'
                         f'{table_html(groups["passing"])}</details>')
        body = "\n".join(parts)

    return PAGE_TEMPLATE.format(title=esc(f"CI status for {login}"), headline=esc(headline), meta=meta, body=body)


def parse_stale_hours(raw):
    raw = raw.strip()
    if not raw:
        return 48
    if not raw.isdigit():
        fail(f"STALE_AFTER_HOURS must be a non-negative whole number, got {raw!r}.")
    return int(raw)


def main():
    token = os.environ.get("DASHBOARD_TOKEN", "").strip()
    if not token:
        fail("No token provided. Create a fine-grained personal access token with read-only Actions access to all repositories and pass it as the token input.")
    anonymize = os.environ.get("ANONYMIZE_PRIVATE", "").strip().lower() != "false"
    salt = os.environ.get("ANONYMIZE_SALT", "")
    out_dir = os.environ.get("OUTPUT_DIR", "").strip() or "_site"
    stale_hours = parse_stale_hours(os.environ.get("STALE_AFTER_HOURS", ""))

    try:
        login = api_get(token, "/user")[0]["login"]
        repos = api_get_all(token, "/user/repos", {"affiliation": "owner", "per_page": 100})
    except ApiError as error:
        fail(f"Listing repositories failed: {error}")
    repos = [r for r in repos if not (r["archived"] or r["fork"] or r.get("disabled"))]

    if anonymize and not salt and any(r["private"] for r in repos):
        fail("Private repositories are anonymized by default, which needs a salt. Set the anonymize-salt input "
             "(for example from `openssl rand -hex 32`), or set anonymize-private to false.")

    rows = []
    for repo in repos:
        hidden = anonymize and repo["private"]
        label = pseudonym(salt, repo["full_name"]) if hidden else repo["name"]
        # The dashboard repository is usually public, and so are its Actions logs: anything printed here must use the label, never the real name.
        try:
            workflows = check_repo(token, repo)
        except ApiError as error:
            fail(f"Reading workflow runs of {label} failed: {error}")
        if workflows:
            rows.append(summarize(repo, label, hidden, workflows))

    generated = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as page:
        page.write(render(login, rows, generated, stale_hours))

    counts = {s: sum(r["state"] == s for r in rows) for s in STATES}
    print(f"Checked {len(repos)} repositories, {len(rows)} with workflow runs: "
          f"{counts['failing']} failing, {counts['pending']} pending, {counts['passing']} passing. Wrote {out_dir}/index.html")


if __name__ == "__main__":
    main()
