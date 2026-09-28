#!/usr/bin/env python3
"""Render the GitHub profile metrics card.

Standard library only. Collects data from the GitHub API (plus WakaTime and
PageSpeed Insights when their tokens are present), samples a palette from the
profile picture, and writes a light and a dark SVG.

Every section is optional: if a source fails, that section is left out and the
rest of the card still renders.

Environment:
  METRICS_TOKEN    personal access token (sees private activity totals)
  GITHUB_TOKEN     fallback token, public data only
  WAKATIME_TOKEN   optional, WakaTime API key
  PAGESPEED_TOKEN  optional, Google PageSpeed Insights API key
"""

import base64
import colorsys
import datetime as dt
import json
import os
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from pathlib import Path

from icons import ICONS

LOGIN = "UPSAtwal"
SITE = "https://uday.codes"
TZ = dt.timezone(dt.timedelta(hours=5, minutes=30), "IST")
HERE = Path(__file__).resolve().parent
OUT = HERE.parent
PALETTE_CACHE = HERE / "palette.json"
WIDTH = 960
PAD = 24
FONT = "-apple-system,BlinkMacSystemFont,Segoe UI,Helvetica,Arial,sans-serif"
MONO = "ui-monospace,SFMono-Regular,SF Mono,Menlo,Consolas,monospace"


def log(*args):
    print(*args, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
#  HTTP
# --------------------------------------------------------------------------- #

def http(url, *, data=None, headers=None, timeout=60, retries=3, raw=False):
    body = json.dumps(data).encode() if data is not None else None
    hdrs = {"User-Agent": f"{LOGIN}-profile-metrics", "Accept": "application/json"}
    hdrs.update(headers or {})
    if body is not None:
        hdrs["Content-Type"] = "application/json"
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=body, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = resp.read()
                return (payload, resp.headers) if raw else json.loads(payload)
        except urllib.error.HTTPError as err:
            last = err
            if err.code in (401, 403, 404, 422):
                break
        except (urllib.error.URLError, TimeoutError) as err:
            last = err
        time.sleep(2 ** attempt)
    raise RuntimeError(f"{url.split('?')[0]}: {last}")


def tokens():
    seen = []
    for name in ("METRICS_TOKEN", "GITHUB_TOKEN"):
        value = os.environ.get(name, "").strip()
        if value and value not in seen:
            seen.append(value)
    return seen


def gh(path, token):
    return http(f"https://api.github.com/{path}",
                headers={"Authorization": f"Bearer {token}",
                         "Accept": "application/vnd.github+json"})


def graphql(query, variables, token):
    res = http("https://api.github.com/graphql", data={"query": query, "variables": variables},
               headers={"Authorization": f"Bearer {token}"})
    if res.get("errors"):
        raise RuntimeError(res["errors"][0].get("message"))
    return res["data"]


# --------------------------------------------------------------------------- #
#  Data collection
# --------------------------------------------------------------------------- #

PROFILE_QUERY = """
query($login: String!) {
  user(login: $login) {
    login name createdAt avatarUrl
    followers { totalCount } following { totalCount }
    organizations { totalCount } starredRepositories { totalCount }
    watching { totalCount } pullRequests { totalCount } issues { totalCount }
    repositoriesContributedTo(contributionTypes: [COMMIT, PULL_REQUEST, ISSUE, REPOSITORY]) { totalCount }
    pinnedItems(first: 4, types: REPOSITORY) {
      nodes { ... on Repository {
        nameWithOwner description stargazerCount forkCount createdAt
        primaryLanguage { name }
      } }
    }
    contributionsCollection {
      totalCommitContributions restrictedContributionsCount
      totalPullRequestContributions totalIssueContributions
      totalPullRequestReviewContributions
      contributionCalendar { totalContributions
        weeks { contributionDays { date contributionCount } } }
    }
  }
}
"""

REPOS_QUERY = """
query($login: String!, $cursor: String) {
  user(login: $login) {
    repositories(ownerAffiliations: OWNER, isFork: false, privacy: PUBLIC,
                 first: 100, after: $cursor) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes {
        nameWithOwner description stargazerCount forkCount createdAt
        primaryLanguage { name }
        licenseInfo { spdxId }
        languages(first: 20, orderBy: { field: SIZE, direction: DESC }) {
          edges { size node { name } }
        }
      }
    }
  }
}
"""


def collect_github():
    last = None
    for token in tokens():
        try:
            user = graphql(PROFILE_QUERY, {"login": LOGIN}, token)["user"]
            repos, cursor = [], None
            while True:
                page = graphql(REPOS_QUERY, {"login": LOGIN, "cursor": cursor}, token)["user"]["repositories"]
                repos += page["nodes"]
                if not page["pageInfo"]["hasNextPage"]:
                    break
                cursor = page["pageInfo"]["endCursor"]
            user["repos"] = repos
            user["pushes"] = collect_pushes(token)
            return user
        except Exception as err:  # try the next token
            last = err
            log("github: token failed:", err)
    raise RuntimeError(f"no working GitHub token: {last}")


def collect_pushes(token):
    """Timestamps of recent pushes. Only times are kept, never repo names."""
    stamps = []
    for page in range(1, 4):
        try:
            events = gh(f"users/{LOGIN}/events?per_page=100&page={page}", token)
        except Exception as err:
            log("events:", err)
            break
        stamps += [e["created_at"] for e in events if e.get("type") == "PushEvent"]
        if len(events) < 100:
            break
    return [dt.datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(TZ) for s in stamps]


def collect_wakatime():
    key = os.environ.get("WAKATIME_TOKEN", "").strip()
    if key:
        auth = base64.b64encode(key.encode()).decode()
        return http("https://wakatime.com/api/v1/users/current/stats/last_7_days",
                    headers={"Authorization": f"Basic {auth}"})["data"]
    return http(f"https://wakatime.com/api/v1/users/{LOGIN.lower()}/stats/last_7_days")["data"]


def collect_pagespeed():
    params = [("url", SITE), ("strategy", "mobile")]
    params += [("category", c) for c in ("performance", "accessibility", "best-practices", "seo")]
    key = os.environ.get("PAGESPEED_TOKEN", "").strip()
    if key:
        params.append(("key", key))
    res = http("https://www.googleapis.com/pagespeedonline/v5/runPagespeed?"
               + urllib.parse.urlencode(params), timeout=120, retries=2)
    lh = res["lighthouseResult"]
    audits = lh["audits"]
    return {
        "scores": [(lh["categories"][k]["title"], round(lh["categories"][k]["score"] * 100))
                   for k in ("performance", "accessibility", "best-practices", "seo")],
        "audits": [(audits[k]["title"], audits[k].get("displayValue", "0"))
                   for k in ("first-contentful-paint", "largest-contentful-paint",
                             "total-blocking-time", "cumulative-layout-shift",
                             "speed-index", "interactive") if k in audits],
    }


# --------------------------------------------------------------------------- #
#  Palette sampled from the profile picture
# --------------------------------------------------------------------------- #

def decode_png(blob):
    """Minimal PNG decoder: non-interlaced, any colour type, 8 or 16 bit."""
    if blob[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    pos, idat, plte, trns = 8, b"", None, None
    while pos < len(blob):
        length, kind = struct.unpack(">I4s", blob[pos:pos + 8])
        chunk = blob[pos + 8:pos + 8 + length]
        pos += 12 + length
        if kind == b"IHDR":
            w, h, depth, ctype, _, _, interlace = struct.unpack(">IIBBBBB", chunk)
        elif kind == b"PLTE":
            plte = [tuple(chunk[i:i + 3]) for i in range(0, len(chunk), 3)]
        elif kind == b"tRNS":
            trns = chunk
        elif kind == b"IDAT":
            idat += chunk
        elif kind == b"IEND":
            break
    if interlace:
        raise ValueError("interlaced PNG")
    if depth < 8 and ctype not in (0, 3):
        raise ValueError("unsupported bit depth")
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[ctype]
    bpp = max(1, channels * depth // 8)
    stride = (w * channels * depth + 7) // 8
    data, prev, rows, i = zlib.decompress(idat), bytearray(stride), [], 0
    for _ in range(h):
        ftype, line = data[i], bytearray(data[i + 1:i + 1 + stride])
        i += 1 + stride
        for x in range(stride):
            a = line[x - bpp] if x >= bpp else 0
            b = prev[x]
            c = prev[x - bpp] if x >= bpp else 0
            if ftype == 1:
                line[x] = (line[x] + a) & 255
            elif ftype == 2:
                line[x] = (line[x] + b) & 255
            elif ftype == 3:
                line[x] = (line[x] + (a + b) // 2) & 255
            elif ftype == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[x] = (line[x] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        rows.append(bytes(line))
        prev = line

    def samples(row):
        if depth == 16:
            return list(row[::2])
        if depth == 8:
            return list(row)
        per, mask, out = 8 // depth, (1 << depth) - 1, []
        for byte in row:
            for k in range(per):
                out.append((byte >> (8 - depth * (k + 1))) & mask)
        return out

    pixels = []
    for row in rows:
        s = samples(row)
        for x in range(w):
            if ctype == 0:
                v = s[x] * 255 // ((1 << depth) - 1) if depth < 8 else s[x]
                pixels.append((v, v, v, 255))
            elif ctype == 2:
                pixels.append((*s[3 * x:3 * x + 3], 255))
            elif ctype == 3:
                idx = s[x]
                alpha = trns[idx] if trns and idx < len(trns) else 255
                pixels.append((*plte[idx], alpha))
            elif ctype == 4:
                pixels.append((s[2 * x], s[2 * x], s[2 * x], s[2 * x + 1]))
            else:
                pixels.append(tuple(s[4 * x:4 * x + 4]))
    return w, h, pixels


def luminance(rgb):
    def lin(c):
        c /= 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def mix(a, b, t):
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def hexc(rgb):
    return "#%02x%02x%02x" % tuple(rgb)


def sample_palette(avatar_url):
    blob, headers = http(avatar_url + "&s=192", raw=True)
    w, h, pixels = decode_png(blob)
    # Average 4x4 blocks first, so dithered or noisy photos give real tones.
    block, cells = 4, []
    for by in range(0, h - block + 1, block):
        for bx in range(0, w - block + 1, block):
            acc, n = [0, 0, 0], 0
            for y in range(by, by + block):
                for x in range(bx, bx + block):
                    r, g, b, a = pixels[y * w + x]
                    if a >= 128:
                        acc[0] += r; acc[1] += g; acc[2] += b; n += 1
            if n:
                cells.append(tuple(v // n for v in acc))
    cells.sort(key=luminance)

    def band(lo, hi):
        part = cells[int(len(cells) * lo):max(int(len(cells) * lo) + 1, int(len(cells) * hi))]
        return tuple(sum(c[i] for c in part) // len(part) for i in range(3))

    dark, mid, light = band(0.02, 0.08), band(0.45, 0.55), band(0.92, 0.98)

    # Accent: the dominant saturated hue, if the picture has one.
    hues = {}
    for c in cells:
        hue, sat, val = colorsys.rgb_to_hsv(*(v / 255 for v in c))
        if sat > 0.3 and 0.2 < val < 0.95:
            hues.setdefault(int(hue * 24) % 24, []).append(c)
    colourful = sum(len(v) for v in hues.values())
    accent = None
    if colourful > len(cells) * 0.03:
        best = max(hues.values(), key=len)
        accent = tuple(sum(c[i] for c in best) // len(best) for i in range(3))

    return {"source": avatar_url, "etag": headers.get("ETag", ""),
            "dark": dark, "mid": mid, "light": light, "accent": accent,
            "monochrome": accent is None}


def theme(sample, mode):
    bg, fg = (sample["dark"], sample["light"]) if mode == "dark" else (sample["light"], sample["dark"])
    # Push toward the extremes until text contrast is comfortable.
    pull = (0, 0, 0) if mode == "dark" else (255, 255, 255)
    push = (255, 255, 255) if mode == "dark" else (0, 0, 0)
    while contrast(bg, fg) < 12:
        bg, fg = mix(bg, pull, 0.15), mix(fg, push, 0.15)
    accent = sample["accent"]
    if accent is None:
        accent = mix(bg, fg, 0.82)
    else:
        while contrast(accent, bg) < 3:
            accent = mix(accent, push, 0.1)
    muted = mix(bg, fg, 0.58)
    return {
        "bg": hexc(bg), "fg": hexc(fg), "muted": hexc(muted), "accent": hexc(accent),
        "border": hexc(mix(bg, fg, 0.16)), "track": hexc(mix(bg, fg, 0.10)),
        "ramp": [hexc(mix(bg, fg, 0.10))] + [hexc(mix(bg, accent, t)) for t in (0.35, 0.55, 0.78, 1.0)],
        "series": [hexc(mix(bg, accent, t)) for t in (1.0, 0.8, 0.62, 0.48)]
                  + [hexc(mix(bg, fg, t)) for t in (0.9, 0.7, 0.5, 0.36)],
    }


def load_palette(avatar_url):
    try:
        sample = sample_palette(avatar_url)
        PALETTE_CACHE.write_text(json.dumps(sample, indent=2) + "\n")
        return sample
    except Exception as err:
        log("palette: sampling failed, using cache:", err)
        return json.loads(PALETTE_CACHE.read_text())


# --------------------------------------------------------------------------- #
#  Derived metrics
# --------------------------------------------------------------------------- #

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def calendar_stats(user):
    cal = user["contributionsCollection"]["contributionCalendar"]
    days = [(dt.date.fromisoformat(d["date"]), d["contributionCount"])
            for w in cal["weeks"] for d in w["contributionDays"]]
    longest = run = 0
    for _, n in days:
        run = run + 1 if n else 0
        longest = max(longest, run)
    current, series = 0, [n for _, n in days]
    if series and series[-1] == 0:  # today may simply not have started yet
        series = series[:-1]
    for n in reversed(series):
        if not n:
            break
        current += 1
    busiest = max(days, key=lambda d: d[1])
    per_weekday = [0] * 7
    for d, n in days:
        per_weekday[d.weekday()] += n
    active = sum(1 for _, n in days if n)
    return {"days": days, "weeks": cal["weeks"], "total": cal["totalContributions"],
            "longest": longest, "current": current, "busiest": busiest,
            "per_weekday": per_weekday, "active": active}


def push_stats(pushes):
    if not pushes:
        return None
    hours, weekdays = [0] * 24, [0] * 7
    for p in pushes:
        hours[p.hour] += 1
        weekdays[p.weekday()] += 1
    night = sum(hours[h] for h in (22, 23, 0, 1, 2, 3, 4))
    peak = max(range(24), key=lambda h: hours[h])
    span = (max(pushes) - min(pushes)).days + 1
    if peak >= 22 or peak < 5:
        persona = "night owl"
    elif peak < 11:
        persona = "early bird"
    elif peak < 18:
        persona = "daytime coder"
    else:
        persona = "evening coder"
    return {"hours": hours, "weekdays": weekdays, "total": len(pushes), "span": span,
            "night_share": round(100 * night / len(pushes)), "peak": peak, "persona": persona}


BUILD_TOOLING = {"Makefile", "CMake", "Dockerfile", "Batchfile", "Procfile", "Nix", "Meson"}


def language_stats(repos, limit=8):
    """Each repository counts equally, split by its own byte mix, so one repo
    full of notebooks or generated build files cannot dominate the picture."""
    totals = {}
    for r in repos:
        edges = [e for e in r["languages"]["edges"] if e["node"]["name"] not in BUILD_TOOLING]
        size = sum(e["size"] for e in edges)
        for e in edges:
            totals[e["node"]["name"]] = totals.get(e["node"]["name"], 0) + e["size"] / size
    ranked = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
    whole = sum(totals.values()) or 1
    top = ranked[:limit]
    rest = sum(v for _, v in ranked[limit:])
    if rest:
        top.append(("Other", rest))
    return [(name, size / whole) for name, size in top], len(totals)


def repo_stats(repos):
    stars = sum(r["stargazerCount"] for r in repos)
    forks = sum(r["forkCount"] for r in repos)
    licences = {}
    for r in repos:
        spdx = (r.get("licenseInfo") or {}).get("spdxId")
        if spdx and spdx != "NOASSERTION":
            licences[spdx] = licences.get(spdx, 0) + 1
    fav = max(licences, key=licences.get) if licences else None
    oldest = min(repos, key=lambda r: r["createdAt"]) if repos else None
    top = max(repos, key=lambda r: r["stargazerCount"]) if repos else None
    return {"stars": stars, "forks": forks, "licence": fav, "oldest": oldest, "top": top}


# --------------------------------------------------------------------------- #
#  SVG drawing
# --------------------------------------------------------------------------- #

def plural(n, word):
    if n == 1:
        return f"1 {word}"
    if word.endswith("y") and word[-2] not in "aeiou":
        return f"{n:,} {word[:-1]}ies"
    return f"{n:,} {word}s"


def esc(text):
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def text(x, y, value, *, size=14, fill="fg", weight=400, anchor="start", family=FONT, extra=""):
    return (f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" font-weight="{weight}" '
            f'text-anchor="{anchor}" font-family="{family}" class="{fill}" {extra}>{esc(value)}</text>')


def icon(name, x, y, cls="accent", scale=1.0):
    return (f'<path transform="translate({x:.1f} {y:.1f}) scale({scale})" '
            f'd="{ICONS[name]}" class="{cls}f"/>')


def wrap(value, width_px, size=13):
    """Greedy word wrap using an average glyph width for a sans-serif face."""
    per_line = max(10, int(width_px / (size * 0.53)))
    lines, line = [], ""
    for word in (value or "").split():
        if line and len(line) + 1 + len(word) > per_line:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        lines.append(line)
    return lines


def stat_line(x, y, icon_name, label):
    return icon(icon_name, x, y - 12) + text(x + 24, y, label)


def section_title(x, y, icon_name, label):
    return icon(icon_name, x, y - 13, "accent", 1.1) + text(x + 26, y, label, size=17, weight=600)


class Card:
    def __init__(self, pal):
        self.p = pal
        self.parts = []
        self.y = PAD

    def add(self, *items):
        self.parts.extend(items)

    def svg(self, footer):
        self.y += 8
        self.add(text(WIDTH - PAD, self.y + 12, footer, size=11, fill="muted", anchor="end"))
        height = self.y + 30
        p = self.p
        style = (f".fg{{fill:{p['fg']}}}.muted{{fill:{p['muted']}}}.accent{{fill:{p['accent']}}}"
                 f".fgf{{fill:{p['fg']}}}.mutedf{{fill:{p['muted']}}}.accentf{{fill:{p['accent']}}}")
        return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{height}" '
                f'viewBox="0 0 {WIDTH} {height}" role="img" aria-label="GitHub metrics for {LOGIN}">'
                f"<style>{style}</style>"
                f'<rect x="0.5" y="0.5" width="{WIDTH - 1}" height="{height - 1}" rx="12" '
                f'fill="{p["bg"]}" stroke="{p["border"]}"/>'
                + "".join(self.parts) + "</svg>\n")


def draw_header(c, user, cal, avatar_b64):
    p, y = c.p, c.y
    c.add(f'<clipPath id="av"><circle cx="{PAD + 28}" cy="{y + 28}" r="28"/></clipPath>',
          f'<image href="data:image/png;base64,{avatar_b64}" x="{PAD}" y="{y}" width="56" height="56" '
          f'clip-path="url(#av)"/>' if avatar_b64 else "",
          f'<circle cx="{PAD + 28}" cy="{y + 28}" r="28" fill="none" stroke="{p["border"]}"/>')
    years = (dt.date.today() - dt.date.fromisoformat(user["createdAt"][:10])).days // 365
    c.add(text(PAD + 72, y + 24, user["name"] or user["login"], size=22, weight=700),
          text(PAD + 72, y + 46, f"@{user['login']} · on GitHub for {years} years · "
               f"{plural(user['followers']['totalCount'], 'follower')}", size=13, fill="muted"))
    # Last 14 days as a strip, like the old card.
    recent = cal["days"][-14:]
    peak = max((n for _, n in recent), default=0) or 1
    x0 = WIDTH - PAD - 14 * 16
    for i, (d, n) in enumerate(recent):
        level = 0 if not n else min(4, 1 + int(3 * n / peak))
        c.add(f'<rect x="{x0 + i * 16}" y="{y + 8}" width="12" height="12" rx="2" '
              f'fill="{p["ramp"][level]}"><title>{d:%d %b}: {n}</title></rect>')
    c.add(text(WIDTH - PAD, y + 44, f"contributed to {plural(user['repositoriesContributedTo']['totalCount'], 'repository')}",
               size=13, fill="muted", anchor="end"))
    c.y += 80


def draw_columns(c, user, repos, rs):
    cc = user["contributionsCollection"]
    cols = [
        ("pulse", "Activity", [
            ("git-commit", f"{cc['totalCommitContributions'] + cc['restrictedContributionsCount']:,} commits in the last year"),
            ("git-pull-request", f"{plural(user['pullRequests']['totalCount'], 'pull request')} opened"),
            ("eye", f"{plural(cc['totalPullRequestReviewContributions'], 'pull request')} reviewed"),
            ("issue-opened", f"{plural(user['issues']['totalCount'], 'issue')} opened"),
        ]),
        ("people", "Community", [
            ("organization", f"member of {plural(user['organizations']['totalCount'], 'organization')}"),
            ("people", f"following {user['following']['totalCount']} people"),
            ("star", f"starred {user['starredRepositories']['totalCount']:,} repositories"),
            ("telescope", f"watching {user['watching']['totalCount']} repositories"),
        ]),
        ("repo", f"{len(repos)} public repositories", [
            ("star", f"{plural(rs['stars'], 'star')} earned"),
            ("repo-forked", plural(rs["forks"], "fork")),
            ("law", f"prefers {rs['licence']}" if rs["licence"] else "no licence preference yet"),
            ("trophy", f"most starred: {rs['top']['nameWithOwner'].split('/')[1]}" if rs["top"] else "no stars yet"),
        ]),
    ]
    colw = (WIDTH - 2 * PAD) / 3
    for i, (ic, title, rows) in enumerate(cols):
        x = PAD + i * colw
        c.add(section_title(x, c.y + 16, ic, title))
        for j, (ri, label) in enumerate(rows):
            c.add(stat_line(x, c.y + 44 + j * 22, ri, label))
    c.y += 44 + 4 * 22 + 10


def draw_calendar(c, cal):
    p = c.p
    c.add(section_title(PAD, c.y + 16, "calendar",
                        f"{cal['total']:,} contributions in the last year · {cal['active']} active days"))
    top, cell, gap = c.y + 48, 13, 3
    weeks = cal["weeks"][-53:]
    counts = sorted(n for _, n in cal["days"] if n)
    cuts = [counts[int(len(counts) * q)] for q in (0.25, 0.5, 0.75)] if counts else [1, 2, 3]
    x0 = PAD + 28
    for wi, week in enumerate(weeks):
        for day in week["contributionDays"]:
            d = dt.date.fromisoformat(day["date"])
            n = day["contributionCount"]
            level = 0 if not n else 1 + sum(n > k for k in cuts)
            c.add(f'<rect x="{x0 + wi * (cell + gap)}" y="{top + ((d.weekday() + 1) % 7) * (cell + gap)}" '
                  f'width="{cell}" height="{cell}" rx="2" fill="{p["ramp"][level]}">'
                  f'<title>{d:%a %d %b %Y}: {n}</title></rect>')
        first = dt.date.fromisoformat(week["contributionDays"][0]["date"])
        if first.day <= 7:
            c.add(text(x0 + wi * (cell + gap), top - 4, f"{first:%b}", size=10, fill="muted"))
    for row, name in ((1, "Mon"), (3, "Wed"), (5, "Fri")):
        c.add(text(PAD, top + row * (cell + gap) + 10, name, size=10, fill="muted"))
    bottom = top + 7 * (cell + gap) + 6
    lx = WIDTH - PAD - 5 * 16 - 60
    c.add(text(lx - 6, bottom + 10, "less", size=10, fill="muted", anchor="end"))
    for i in range(5):
        c.add(f'<rect x="{lx + i * 16}" y="{bottom}" width="12" height="12" rx="2" fill="{p["ramp"][i]}"/>')
    c.add(text(lx + 5 * 16 + 2, bottom + 10, "more", size=10, fill="muted"))
    busiest_day, busiest_n = cal["busiest"]
    best_weekday = WEEKDAYS[max(range(7), key=lambda i: cal["per_weekday"][i])]
    facts = [("flame", f"current streak {plural(cal['current'], 'day')}"),
             ("trophy", f"longest streak {plural(cal['longest'], 'day')}"),
             ("zap", f"busiest day {busiest_day:%d %b} ({busiest_n})"),
             ("heart", f"favourite day {best_weekday}")]
    fy = bottom + 36
    colw = (WIDTH - 2 * PAD) / 4
    for i, (ic, label) in enumerate(facts):
        c.add(stat_line(PAD + i * colw, fy, ic, label))
    c.y = fy + 18


def draw_languages(c, langs, count):
    p = c.p
    c.add(section_title(PAD, c.y + 16, "code", f"{count} languages · each public repository weighted equally"))
    bar_y, bar_w, x = c.y + 32, WIDTH - 2 * PAD, PAD

    def shade(i, name):
        return p["border"] if name == "Other" else p["series"][i % len(p["series"])]

    for i, (name, share) in enumerate(langs):
        w = max(0.0, bar_w * share - 2)
        c.add(f'<rect x="{x:.1f}" y="{bar_y}" width="{w:.1f}" height="10" rx="3" '
              f'fill="{shade(i, name)}"><title>{esc(name)} {share:.1%}</title></rect>')
        x += bar_w * share
    colw = (WIDTH - 2 * PAD) / 4
    for i, (name, share) in enumerate(langs):
        lx, ly = PAD + (i % 4) * colw, bar_y + 36 + (i // 4) * 22
        c.add(f'<circle cx="{lx + 5}" cy="{ly - 5}" r="5" fill="{shade(i, name)}"/>',
              text(lx + 16, ly, name),
              text(lx + colw - 16, ly, f"{share:.1%}", size=12, fill="muted", anchor="end"))
    c.y = bar_y + 36 + ((len(langs) + 3) // 4) * 22 + 4


def draw_rhythm(c, ps):
    p = c.p
    c.add(section_title(PAD, c.y + 16, "clock",
                        f"when i push · {ps['total']} pushes over the last {ps['span']} days"))
    top, height = c.y + 40, 84
    left_w = (WIDTH - 2 * PAD) * 0.62
    bw = left_w / 24
    peak = max(ps["hours"]) or 1
    for h, n in enumerate(ps["hours"]):
        bh = max(2, height * n / peak) if n else 2
        fill = p["accent"] if h == ps["peak"] else (p["series"][2] if n else p["track"])
        c.add(f'<rect x="{PAD + h * bw + 1:.1f}" y="{top + height - bh:.1f}" width="{bw - 3:.1f}" '
              f'height="{bh:.1f}" rx="2" fill="{fill}"><title>{h:02d}:00 IST: {n}</title></rect>')
        if h % 3 == 0:
            c.add(text(PAD + h * bw + bw / 2, top + height + 16, f"{h:02d}", size=10, fill="muted", anchor="middle"))
    rx = PAD + left_w + 36
    rw = WIDTH - PAD - rx
    wpeak = max(ps["weekdays"]) or 1
    for i, n in enumerate(ps["weekdays"]):
        yy = top + i * 12
        c.add(text(rx, yy + 8, WEEKDAYS[i][:3], size=10, fill="muted"),
              f'<rect x="{rx + 34}" y="{yy}" width="{rw - 34}" height="7" rx="3" fill="{p["track"]}"/>',
              f'<rect x="{rx + 34}" y="{yy}" width="{max(3, (rw - 34) * n / wpeak):.1f}" height="7" rx="3" '
              f'fill="{p["series"][1]}"><title>{WEEKDAYS[i]}: {n}</title></rect>')
    note = (f"{ps['persona']} · busiest hour {ps['peak']:02d}:00 IST · "
            f"{ps['night_share']}% of pushes land after 10pm")
    c.add(stat_line(PAD, top + height + 42, "moon" if ps["persona"] in ("night owl", "evening coder") else "sun", note))
    c.y = top + height + 58


def draw_repos(c, repos):
    c.add(section_title(PAD, c.y + 16, "book", "featured repositories"))
    colw = (WIDTH - 2 * PAD - 24) / 2
    y0 = c.y + 40
    heights = [0, 0]
    for i, r in enumerate(repos[:4]):
        col = i % 2
        x = PAD + col * (colw + 24)
        y = y0 + heights[col]
        c.add(icon("repo", x, y - 12), text(x + 24, y, r["nameWithOwner"], weight=600, fill="accent"))
        lines = wrap(r.get("description") or "", colw - 24, 12)[:3]
        for k, line in enumerate(lines):
            c.add(text(x + 24, y + 20 + k * 17, line, size=12, fill="muted"))
        meta_y = y + 20 + len(lines) * 17 + 4
        lang = (r.get("primaryLanguage") or {}).get("name", "")
        meta = " · ".join(filter(None, [lang, plural(r["stargazerCount"], "star"), plural(r["forkCount"], "fork")]))
        c.add(text(x + 24, meta_y, meta, size=12))
        heights[col] += meta_y - y + 30
    c.y = y0 + max(heights) - 12


def gauge(cx, cy, score, label, p):
    r, circ = 26, 2 * 3.14159 * 26
    return (f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{p["track"]}" stroke-width="5"/>'
            f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{p["accent"]}" stroke-width="5" '
            f'stroke-linecap="round" stroke-dasharray="{circ * score / 100:.1f} {circ:.1f}" '
            f'transform="rotate(-90 {cx} {cy})"/>'
            + text(cx, cy + 6, score, size=17, weight=700, anchor="middle", family=MONO)
            + text(cx, cy + 50, label, size=12, fill="muted", anchor="middle"))


def draw_pagespeed(c, ps):
    p = c.p
    c.add(section_title(PAD, c.y + 16, "rocket", f"PageSpeed Insights · {SITE.split('//')[1]} (mobile)"))
    colw = (WIDTH - 2 * PAD) / 4
    cy = c.y + 72
    for i, (label, score) in enumerate(ps["scores"]):
        c.add(gauge(PAD + colw * i + colw / 2, cy, score, label, p))
    ay = cy + 84
    half = (WIDTH - 2 * PAD - 24) / 2
    for i, (label, value) in enumerate(ps["audits"]):
        x = PAD + (i % 2) * (half + 24)
        yy = ay + (i // 2) * 22
        c.add(stat_line(x, yy, "zap", label), text(x + half, yy, value, size=13, fill="muted", anchor="end"))
    c.y = ay + ((len(ps["audits"]) + 1) // 2) * 22 - 4


def draw_wakatime(c, wk):
    p = c.p
    c.add(section_title(PAD, c.y + 16, "terminal", "WakaTime · the last 7 days"))
    y = c.y + 44
    facts = []
    if wk.get("total_seconds"):
        facts += [("clock", f"{wk['human_readable_total']} this week"),
                  ("graph", f"{wk['human_readable_daily_average']} a day")]
    if wk.get("editors"):
        facts.append(("code", f"mostly in {wk['editors'][0]['name']}"))
    if wk.get("operating_systems"):
        facts.append(("device-desktop", f"on {wk['operating_systems'][0]['name']}"))
    colw = (WIDTH - 2 * PAD) / 4
    for i, (ic, label) in enumerate(facts):
        c.add(stat_line(PAD + i * colw, y, ic, label))
    langs = [lang for lang in wk.get("languages", [])
             if lang.get("percent", 0) > 0 and lang.get("name") != "Other"][:6]
    by = y + 22 if facts else c.y + 34
    barw = WIDTH - 2 * PAD - 140 - 180
    for i, lang in enumerate(langs):
        yy = by + i * 20
        c.add(text(PAD, yy + 9, lang["name"], size=12),
              f'<rect x="{PAD + 140}" y="{yy}" width="{barw}" height="8" rx="4" fill="{p["track"]}"/>',
              f'<rect x="{PAD + 140}" y="{yy}" width="{max(4, barw * lang["percent"] / 100):.1f}" height="8" '
              f'rx="4" fill="{p["series"][i % len(p["series"])]}"/>',
              text(WIDTH - PAD, yy + 9, " · ".join(filter(None, [lang.get("text") if wk.get("total_seconds") else None,
                                     f"{lang['percent']:.0f}%"])),
                   size=12, fill="muted", anchor="end"))
    c.y = by + len(langs) * 20 + 4


def gap(c, n=26):
    c.y += n


# --------------------------------------------------------------------------- #
#  Main
# --------------------------------------------------------------------------- #

def safe(label, fn, *args):
    try:
        return fn(*args)
    except Exception as err:
        log(f"{label}: skipped ({err})")
        return None


def main():
    user = collect_github()
    cal = calendar_stats(user)
    langs, lang_count = language_stats(user["repos"])
    rs = repo_stats(user["repos"])
    ps = push_stats(user["pushes"])
    wk = safe("wakatime", collect_wakatime)
    speed = safe("pagespeed", collect_pagespeed)
    sample = load_palette(user["avatarUrl"])
    avatar = safe("avatar", lambda: base64.b64encode(
        http(user["avatarUrl"] + "&s=112", raw=True)[0]).decode())

    featured = [r for r in user["pinnedItems"]["nodes"] if r]
    if len(featured) < 4:
        pinned = {r["nameWithOwner"] for r in featured}
        extra = sorted((r for r in user["repos"] if r["nameWithOwner"] not in pinned
                        and r["nameWithOwner"].lower() != f"{LOGIN}/{LOGIN}".lower()),
                       key=lambda r: (r["stargazerCount"], r["createdAt"]), reverse=True)
        featured += extra[:4 - len(featured)]

    now = dt.datetime.now(TZ)
    footer = f"updated {now:%d %b %Y, %H:%M} IST · rendered by a zero-dependency script · uday.codes"
    for mode in ("dark", "light"):
        c = Card(theme(sample, mode))
        draw_header(c, user, cal, avatar); gap(c, 8)
        draw_columns(c, user, user["repos"], rs); gap(c)
        draw_calendar(c, cal); gap(c)
        draw_languages(c, langs, lang_count); gap(c)
        if ps:
            draw_rhythm(c, ps); gap(c)
        if featured:
            draw_repos(c, featured); gap(c)
        if speed:
            draw_pagespeed(c, speed); gap(c)
        if wk and wk.get("languages") is not None:
            draw_wakatime(c, wk); gap(c, 12)
        (OUT / f"metrics-{mode}.svg").write_text(c.svg(footer))
        log(f"wrote metrics-{mode}.svg")
    mono = "monochrome" if sample["monochrome"] else "colour"
    log(f"palette: {mono}, dark={hexc(sample['dark'])} mid={hexc(sample['mid'])} light={hexc(sample['light'])}")


if __name__ == "__main__":
    main()
