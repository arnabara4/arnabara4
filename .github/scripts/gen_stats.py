#!/usr/bin/env python3
"""Generate the animated stat cards used by the profile README.

Everything is rendered locally from the GitHub GraphQL API and committed to the
repo, so the README never depends on a third-party widget host staying alive.

Outputs:
  assets/stats.svg  - contribution metrics + 12-month activity sparkline
  assets/langs.svg  - language distribution across owned repositories

Usage:  GH_TOKEN=<token> python3 .github/scripts/gen_stats.py
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

USER = os.environ.get("PROFILE_USER", "arnabara4")
TOKEN = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
API = "https://api.github.com/graphql"
ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "assets"

# Bulk HTML archive from 2023 - 4.1 MB of vendored markup that would otherwise
# drown out every real language. Excluded so the card reflects actual work.
REPO_DENYLIST = {"Projects"}
# Markup/config that is not a programming language.
LANG_DENYLIST = {"HTML", "CSS", "SCSS", "Less", "Dockerfile", "Makefile", "Shell", "Batchfile"}

FIRST_YEAR = 2023

PALETTE = {
    "TypeScript": "#3178c6",
    "Python": "#3572A5",
    "JavaScript": "#f1e05a",
    "C++": "#f34b7d",
    "C#": "#178600",
    "C": "#555555",
    "Java": "#b07219",
    "Go": "#00ADD8",
    "Rust": "#dea584",
    "Kotlin": "#A97BFF",
    "Swift": "#F05138",
    "PLpgSQL": "#336790",
    "Solidity": "#AA6746",
    "Jupyter Notebook": "#DA5B0B",
    "Nunjucks": "#3d8137",
    "Vue": "#41b883",
    "Ruby": "#701516",
    "PHP": "#4F5D95",
}
FALLBACK_COLORS = ["#7dd3fc", "#a78bfa", "#f472b6", "#fbbf24", "#34d399", "#fb923c"]


def gql(query: str, variables: dict) -> dict:
    if not TOKEN:
        sys.exit("error: GH_TOKEN / GITHUB_TOKEN is required")
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        API,
        data=body,
        headers={
            "Authorization": f"bearer {TOKEN}",
            "Content-Type": "application/json",
            "User-Agent": f"{USER}-profile-stats",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.load(resp)
    except urllib.error.HTTPError as exc:  # surface the API's own message
        sys.exit(f"error: GitHub API {exc.code}: {exc.read().decode()[:400]}")
    if "errors" in payload:
        sys.exit(f"error: GraphQL: {json.dumps(payload['errors'])[:400]}")
    return payload["data"]


PROFILE_Q = """
query($login:String!){
  user(login:$login){
    name login createdAt
    followers{totalCount}
    repositories(first:100, ownerAffiliations:OWNER, isFork:false){
      totalCount
      nodes{ name stargazerCount languages(first:12, orderBy:{field:SIZE, direction:DESC}){
        edges{ size node{ name color } } } }
    }
  }
}
"""

YEAR_Q = """
query($login:String!,$from:DateTime!,$to:DateTime!){
  user(login:$login){
    contributionsCollection(from:$from,to:$to){
      totalCommitContributions
      totalPullRequestContributions
      totalPullRequestReviewContributions
      totalIssueContributions
      restrictedContributionsCount
      contributionCalendar{ totalContributions }
    }
  }
}
"""

CAL_Q = """
query($login:String!,$from:DateTime!,$to:DateTime!){
  user(login:$login){
    contributionsCollection(from:$from,to:$to){
      contributionCalendar{
        totalContributions
        weeks{ contributionDays{ contributionCount date } }
      }
    }
  }
}
"""

# `user.pullRequests.totalCount` reports PRs across repositories the viewer
# cannot enumerate, so it can never be reconciled against a visible list.
# Paginate the ordered connection instead and count what is actually there.
MERGED_Q = """
query($login:String!,$cursor:String){
  user(login:$login){
    pullRequests(states:MERGED, first:100, after:$cursor,
                 orderBy:{field:CREATED_AT, direction:DESC}){
      pageInfo{ hasNextPage endCursor }
      nodes{ repository{ nameWithOwner } }
    }
  }
}
"""


def merged_pr_stats() -> tuple[int, set[str]]:
    """(merged PRs, distinct `owner/name` repos) across all visible repos."""
    total, repos, cursor = 0, set(), None
    while True:
        page = gql(MERGED_Q, {"login": USER, "cursor": cursor})["user"]["pullRequests"]
        total += len(page["nodes"])
        repos.update(n["repository"]["nameWithOwner"] for n in page["nodes"])
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]
    return total, repos


def repo_languages(full_name: str) -> dict[str, int]:
    """REST language byte counts; empty dict when the repo is unreadable."""
    req = urllib.request.Request(
        f"https://api.github.com/repos/{full_name}/languages",
        headers={
            "Authorization": f"bearer {TOKEN}",
            "Accept": "application/vnd.github+json",
            "User-Agent": f"{USER}-profile-stats",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
        return {}


def collect() -> dict:
    prof = gql(PROFILE_Q, {"login": USER})["user"]

    totals = {"commits": 0, "prs": 0, "reviews": 0, "issues": 0, "contribs": 0}
    now = datetime.now(timezone.utc)
    for year in range(FIRST_YEAR, now.year + 1):
        frm = datetime(year, 1, 1, tzinfo=timezone.utc)
        to = min(datetime(year, 12, 31, 23, 59, 59, tzinfo=timezone.utc), now)
        if frm > now:
            break
        cc = gql(YEAR_Q, {"login": USER, "from": frm.isoformat(), "to": to.isoformat()})
        cc = cc["user"]["contributionsCollection"]
        totals["commits"] += cc["totalCommitContributions"]
        totals["prs"] += cc["totalPullRequestContributions"]
        totals["reviews"] += cc["totalPullRequestReviewContributions"]
        totals["issues"] += cc["totalIssueContributions"]
        totals["contribs"] += cc["contributionCalendar"]["totalContributions"]

    # rolling 12 months for the sparkline
    frm = now - timedelta(days=364)
    cal = gql(CAL_Q, {"login": USER, "from": frm.isoformat(), "to": now.isoformat()})
    cal = cal["user"]["contributionsCollection"]["contributionCalendar"]
    days = [d for w in cal["weeks"] for d in w["contributionDays"]]

    stars = sum(r["stargazerCount"] for r in prof["repositories"]["nodes"])
    merged, pr_repos = merged_pr_stats()
    pr_orgs = {r.split("/", 1)[0] for r in pr_repos}

    # Language mix over every repository he actually ships into - the repos he
    # owns plus the ones he has landed merged PRs in. Owned-only would report
    # whichever side project happens to have the most bytes.
    scanned = {f"{USER}/{r['name']}" for r in prof["repositories"]["nodes"]} | pr_repos
    scanned = {r for r in scanned if r.split("/", 1)[1] not in REPO_DENYLIST}

    langs: dict[str, int] = {}
    for full_name in sorted(scanned):
        for name, size in repo_languages(full_name).items():
            if name in LANG_DENYLIST:
                continue
            langs[name] = langs.get(name, 0) + size

    return {
        "name": prof["name"] or prof["login"],
        "followers": prof["followers"]["totalCount"],
        "repos": prof["repositories"]["totalCount"],
        "merged_prs": merged,
        "pr_repos": len(pr_repos),
        "pr_orgs": len(pr_orgs),
        "scanned": len(scanned),
        "stars": stars,
        "totals": totals,
        "year_total": cal["totalContributions"],
        "days": days,
        "langs": langs,
    }


def spark_paths(days: list[dict], x: float, y: float, w: float, h: float) -> tuple[str, str]:
    """Weekly-bucketed area + line path for the contribution sparkline."""
    if not days:
        return "", ""
    weeks: list[int] = []
    for i in range(0, len(days), 7):
        weeks.append(sum(d["contributionCount"] for d in days[i : i + 7]))
    peak = max(weeks) or 1
    n = len(weeks)
    step = w / max(n - 1, 1)
    pts = [(x + i * step, y + h - (v / peak) * h) for i, v in enumerate(weeks)]

    # Catmull-Rom -> cubic bezier for a smooth curve
    d = [f"M{pts[0][0]:.1f},{pts[0][1]:.1f}"]
    for i in range(len(pts) - 1):
        p0 = pts[i - 1] if i else pts[i]
        p1, p2 = pts[i], pts[i + 1]
        p3 = pts[i + 2] if i + 2 < len(pts) else p2
        c1 = (p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6)
        c2 = (p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6)
        d.append(f"C{c1[0]:.1f},{c1[1]:.1f} {c2[0]:.1f},{c2[1]:.1f} {p2[0]:.1f},{p2[1]:.1f}")
    line = " ".join(d)
    area = f"{line} L{pts[-1][0]:.1f},{y + h:.1f} L{pts[0][0]:.1f},{y + h:.1f} Z"
    return area, line


SHELL_STYLE = """
    .sans{font-family:'Segoe UI',Ubuntu,'Helvetica Neue',Arial,sans-serif}
    .mono{font-family:'JetBrains Mono','Fira Code',ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
    @keyframes rise{0%{opacity:0;transform:translateY(10px)}100%{opacity:1;transform:translateY(0)}}
    @keyframes glow{0%,100%{opacity:.30}50%{opacity:.70}}
    .halo{animation:glow 6s ease-in-out infinite}
"""


def card_shell(w: int, h: int, title: str, kicker: str, extra_defs: str, extra_style: str, body: str) -> str:
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img" aria-label="{escape(title)}">
  <title>{escape(title)}</title>
  <defs>
    <linearGradient id="cbg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#0a101c"/><stop offset="100%" stop-color="#060a12"/>
    </linearGradient>
    <linearGradient id="accent" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0%" stop-color="#38bdf8"/><stop offset="50%" stop-color="#a78bfa"/><stop offset="100%" stop-color="#f472b6"/>
    </linearGradient>
    <radialGradient id="halo" cx="50%" cy="50%" r="50%">
      <stop offset="0%" stop-color="#2563eb" stop-opacity="0.35"/><stop offset="100%" stop-color="#2563eb" stop-opacity="0"/>
    </radialGradient>
    <clipPath id="cclip"><rect width="{w}" height="{h}" rx="16"/></clipPath>
{extra_defs}
  </defs>
  <style>{SHELL_STYLE}{extra_style}</style>
  <g clip-path="url(#cclip)">
    <rect width="{w}" height="{h}" fill="url(#cbg)"/>
    <ellipse class="halo" cx="{w - 40}" cy="-10" rx="240" ry="150" fill="url(#halo)"/>
    <text class="mono" x="26" y="34" font-size="11.5" letter-spacing="2.6" fill="#5c78a8">{escape(kicker)}</text>
    <text class="sans" x="26" y="60" font-size="20" font-weight="700" fill="#e6edf7">{escape(title)}</text>
    <rect x="26" y="72" width="54" height="3" rx="1.5" fill="url(#accent)"/>
{body}
    <rect x="0.5" y="0.5" width="{w - 1}" height="{h - 1}" rx="16" fill="none" stroke="#1e2b45"/>
  </g>
</svg>
"""


def fmt(n: int) -> str:
    return f"{n:,}"


def build_stats(d: dict) -> str:
    w, h = 620, 330
    t = d["totals"]
    # Two scopes, labelled: PR data covers every repo the account touches
    # (including private orgs); calendar counters are public-only because
    # GitHub gates private contributions behind a profile setting.
    groups = [
        ("ALL REPOSITORIES  ·  INCLUDING PRIVATE ORGS", [
            ("Merged PRs", fmt(d["merged_prs"]), "#34d399"),
            ("Repos shipped to", fmt(d["pr_repos"]), "#7dd3fc"),
            ("Orgs shipped to", fmt(d["pr_orgs"]), "#fbbf24"),
        ]),
        ("PUBLIC ACTIVITY", [
            ("Contributions", fmt(t["contribs"]), "#a78bfa"),
            ("Commits", fmt(t["commits"]), "#f472b6"),
            ("Repos owned", fmt(d["repos"]), "#fb923c"),
        ]),
    ]

    body: list[str] = []
    x0, cw, ch = 26, 190, 58
    y = 92
    idx = 0
    for heading, tiles in groups:
        body.append(
            f'    <text class="mono" x="{x0}" y="{y}" font-size="9.5" letter-spacing="1.4" '
            f'fill="#4d648c">{escape(heading)}</text>'
        )
        y += 10
        for i, (label, value, colour) in enumerate(tiles):
            cx = x0 + i * (cw + 8)
            body.append(
                f'    <g style="opacity:0;animation:rise .6s ease-out {0.08 * idx:.2f}s forwards">'
                f'<rect x="{cx}" y="{y}" width="{cw}" height="{ch}" rx="10" fill="#0e1626" stroke="#1c2942"/>'
                f'<rect x="{cx}" y="{y}" width="3" height="{ch}" rx="1.5" fill="{colour}"/>'
                f'<text class="sans" x="{cx + 16}" y="{y + 26}" font-size="23" font-weight="800" fill="#f0f6ff">{value}</text>'
                f'<text class="mono" x="{cx + 16}" y="{y + 44}" font-size="10.5" letter-spacing="1.1" fill="#6b83aa">{escape(label.upper())}</text>'
                "</g>"
            )
            idx += 1
        y += ch + 18

    sx, sy, sw, sh = 26, 270, w - 52, 38
    area, line = spark_paths(d["days"], sx, sy, sw, sh)
    body.append(
        f'    <text class="mono" x="{sx}" y="{sy - 8}" font-size="10.5" letter-spacing="1.1" fill="#6b83aa">'
        f'LAST 12 MONTHS<tspan fill="#38bdf8" dx="8">{fmt(d["year_total"])} PUBLIC CONTRIBUTIONS</tspan></text>'
    )
    if area:
        body.append(f'    <path d="{area}" fill="url(#sparkFill)" style="opacity:0;animation:rise .9s ease-out .55s forwards"/>')
        body.append(
            f'    <path class="spark" d="{line}" fill="none" stroke="url(#accent)" stroke-width="2.2" '
            'stroke-linecap="round" stroke-linejoin="round"/>'
        )

    extra_defs = """    <linearGradient id="sparkFill" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#38bdf8" stop-opacity="0.34"/><stop offset="100%" stop-color="#38bdf8" stop-opacity="0"/>
    </linearGradient>"""
    extra_style = """
    @keyframes draw{to{stroke-dashoffset:0}}
    .spark{stroke-dasharray:2600;stroke-dashoffset:2600;animation:draw 2.6s ease-out .3s forwards}
"""
    return card_shell(w, h, "Contribution metrics", "GITHUB ACTIVITY", extra_defs, extra_style, "\n".join(body))


def build_langs(d: dict) -> str:
    w, h = 620, 330
    langs = sorted(d["langs"].items(), key=lambda kv: -kv[1])
    if not langs:
        langs = [("No data", 1)]
    grand = sum(v for _, v in langs) or 1
    # Anything under half a percent is noise - a stray config file, a vendored
    # snippet. Rolling it into "Other" beats printing a row that reads 0.0%.
    top = [(k, v) for k, v in langs[:6] if v / grand >= 0.005]
    rest = grand - sum(v for _, v in top)
    if rest / grand >= 0.005:
        top.append(("Other", rest))
    total = sum(v for _, v in top) or 1

    def colour(name: str, idx: int) -> str:
        return PALETTE.get(name) or FALLBACK_COLORS[idx % len(FALLBACK_COLORS)]

    body: list[str] = []
    bx, by, bw, bh = 26, 100, w - 52, 16
    body.append(f'    <rect x="{bx}" y="{by}" width="{bw}" height="{bh}" rx="8" fill="#0e1626" stroke="#1c2942"/>')
    body.append(f'    <clipPath id="barclip"><rect x="{bx}" y="{by}" width="{bw}" height="{bh}" rx="8"/></clipPath>')
    body.append('    <g clip-path="url(#barclip)">')
    cursor = float(bx)
    for i, (name, size) in enumerate(top):
        seg = bw * size / total
        body.append(
            f'      <rect x="{cursor:.1f}" y="{by}" width="{seg:.1f}" height="{bh}" fill="{colour(name, i)}" '
            f'style="transform-origin:{cursor:.1f}px 0;animation:grow .8s cubic-bezier(.2,.8,.2,1) {0.06 * i:.2f}s both"/>'
        )
        cursor += seg
    body.append("    </g>")

    lx, ly = 26, 150
    for i, (name, size) in enumerate(top):
        col = lx + (i % 2) * 290
        row = ly + (i // 2) * 38
        pct = 100 * size / total
        body.append(
            f'    <g style="opacity:0;animation:rise .55s ease-out {0.07 * i:.2f}s forwards">'
            f'<circle cx="{col + 7}" cy="{row + 8}" r="5.5" fill="{colour(name, i)}"/>'
            f'<text class="sans" x="{col + 22}" y="{row + 13}" font-size="14" font-weight="600" fill="#dbe6f5">{escape(name)}</text>'
            f'<text class="mono" x="{col + 250}" y="{row + 13}" font-size="12.5" text-anchor="end" fill="#7f97bd">{pct:.1f}%</text>'
            f'<rect x="{col + 22}" y="{row + 20}" width="228" height="3" rx="1.5" fill="#16223a"/>'
            f'<rect x="{col + 22}" y="{row + 20}" width="{228 * size / top[0][1]:.1f}" height="3" rx="1.5" fill="{colour(name, i)}" '
            f'style="transform-origin:{col + 22}px 0;animation:grow .8s cubic-bezier(.2,.8,.2,1) {0.07 * i:.2f}s both"/>'
            "</g>"
        )

    extra_style = """
    @keyframes grow{0%{transform:scaleX(0)}100%{transform:scaleX(1)}}
"""
    kicker = f"ACROSS {d['scanned']} REPOSITORIES  ·  OWNED + CONTRIBUTED"
    return card_shell(w, h, "Language distribution", kicker, "", extra_style, "\n".join(body))


def main() -> None:
    data = collect()
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "stats.svg").write_text(build_stats(data), encoding="utf-8")
    (OUT / "langs.svg").write_text(build_langs(data), encoding="utf-8")
    t = data["totals"]
    print(
        f"wrote assets/stats.svg + assets/langs.svg  "
        f"(contribs={t['contribs']} commits={t['commits']} prs={t['prs']} "
        f"merged={data['merged_prs']} pr_repos={data['pr_repos']} "
        f"pr_orgs={data['pr_orgs']} repos={data['repos']} langs={len(data['langs'])})"
    )


if __name__ == "__main__":
    main()
