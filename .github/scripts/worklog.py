#!/usr/bin/env python3
"""
work-log automation (standard library only).

  python3 .github/scripts/worklog.py log   [--at-hour 18] [--force] [--start ISO] [--end ISO]
  python3 .github/scripts/worklog.py plan  [--at-hour 8]  [--force]
  python3 .github/scripts/worklog.py week  [--at-hour 18] [--force] [--start ISO]

log  : rolling window from the end of the previous log to now. Collects every
       issue comment and open/close/reopen event, grouped by parent issue.
       Output: logs/YYYY/YYYY-MM-DD.md. No AI anywhere in this script; issue bodies are skipped.
plan : today's plan from the Project "Status" field (In Progress / Todo /
       Blocked), with each item's latest comment, activity since the last log,
       and stale items. Output: plans/YYYY/YYYY-MM-DD.md.
boards: board index (boards/index.md, boards.json): every board ID, normalized,
       with all mentions across issues and comments. Comment text is not changed.
week : weekly report (Fridays). Rolling window since the previous report:
       per-workstream counts, issues closed / opened, stale items, and every
       comment grouped by parent issue. Output: reports/YYYY/YYYY-Www.md.

Environment:
  GITHUB_TOKEN     required (provided automatically in Actions)
  REPO             owner/name, default lkebnl/llm-learning
  PROJECT_TOKEN    optional, classic PAT with read:project (needed for Status)
  PROJECT_OWNER    optional, project owner login, default = REPO owner
  PROJECT_NUMBER   optional, the N in .../projects/N
  REPORT_LANG      en (default) or zh: language of the log and weekly report
  WORKLOG_DAYS     "weekdays" (default) or "all": also run on Saturday/Sunday
  WEEK_DAY         weekday of the weekly report, 0=Mon … 6=Sun, default 4 (Friday)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
UTC = dt.timezone.utc
API = "https://api.github.com"
REPO = os.environ.get("REPO") or "lkebnl/llm-learning"
TOKEN = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
ROOT = Path(os.environ.get("WORKLOG_ROOT", "."))
STATE = ROOT / "logs" / ".state.json"
ALL_DAYS = os.environ.get("WORKLOG_DAYS", "weekdays").lower() == "all"
WEEK_DAY = int(os.environ.get("WEEK_DAY", "4"))
WEEKDAY = "一二三四五六日"
# Output language for the log and weekly report: REPORT_LANG=en (default) or zh.
LANG = "zh" if os.environ.get("REPORT_LANG", "en").lower().startswith("zh") else "en"
TXT = {
    "en": {
        "opened": "opened", "closed": "closed", "reopened": "reopened", "comment": "comment", "state": "status",
        "log_title": "Work log {day}", "week_title": "Weekly report {wk} ({a} – {b})",
        "win_ny": "Window (New York)", "win_utc": "Window (UTC)",
        "summary": "Issues with activity: {n} (opened {o}, closed {c}); comments: {m}",
        "source": "Source: issue comments and status events fetched by script, no AI; issue bodies not included",
        "idle_mark": " (no update in this window)", "idle_h": "Open issues with no update in this window",
        "none": "(none)", "charts": "Charts", "daily_alt": "Daily activity", "effort_alt": "Weekly effort",
        "table_h": "Workstreams", "table_cols": "| Workstream | Comments | Opened | Closed | Issues with activity |",
        "closed_h": "Closed this week", "opened_h": "Opened this week",
        "stale_h": "Open issues with no update for over {d} days", "last_upd": "last updated {d}",
        "details_h": "Details",
    },
    "zh": {
        "opened": "新建", "closed": "关闭", "reopened": "重新打开", "comment": "评论", "state": "状态",
        "log_title": "工作记录 {day}", "week_title": "周报 {wk}（{a} – {b}）",
        "win_ny": "窗口（纽约）", "win_utc": "窗口（UTC）",
        "summary": "有活动的 issue：{n}（新建 {o}，关闭 {c}）；评论：{m}",
        "source": "来源：脚本直接抓取 issue 评论与状态事件，未经 AI 处理；issue 正文不收录",
        "idle_mark": "（本窗口无更新）", "idle_h": "本窗口无更新的 open issue",
        "none": "（无）", "charts": "图表", "daily_alt": "每日活跃度", "effort_alt": "每周精力分布",
        "table_h": "各工作线概况", "table_cols": "| 工作线 | 评论 | 新建 | 关闭 | 有活动的 issue |",
        "closed_h": "本周关闭", "opened_h": "本周新建",
        "stale_h": "超过 {d} 天无更新的 open issue", "last_upd": "最后更新 {d}",
        "details_h": "本周明细",
    },
}


def T(key, **kw):
    return TXT[LANG][key].format(**kw)


def ev_label(e):
    """Status-event text in the output language, e.g. 'closed (completed)' / '关闭（completed）'."""
    base = T(e["ev"])
    if e.get("reason"):
        return f"{base} ({e['reason']})" if LANG == "en" else f"{base}（{e['reason']}）"
    return base
STALE_DAYS = 7


# ---------------------------------------------------------------- GitHub API
def _request(url, data=None, token=None):
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "work-log-bot",
    }
    tok = token or TOKEN
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r), r.headers.get("Link", "")


def _next(link):
    m = re.search(r'<([^>]+)>;\s*rel="next"', link or "")
    return m.group(1) if m else None


def get(path, **params):
    """GET with automatic pagination for list endpoints."""
    url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
    data, link = _request(url)
    if not isinstance(data, list):
        return data
    out = list(data)
    while (url := _next(link)):
        data, link = _request(url)
        out.extend(data)
    return out


# ---------------------------------------------------------------- helpers
def ts(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def iso(t):
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def nyf(t, fmt="%m-%d %H:%M"):
    return t.astimezone(NY).strftime(fmt)


def day_title(t):
    d = t.astimezone(NY)
    if LANG == "en":
        return f"{d:%Y-%m-%d} ({d:%a})"
    return f"{d:%Y-%m-%d}（周{WEEKDAY[d.weekday()]}）"


def snippet(text, n=140):
    s = re.sub(r"\s+", " ", text or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def quote(text):
    lines = (text or "").strip().splitlines() or [""]
    return "\n".join("  > " + ln if ln.strip() else "  >" for ln in lines)


def load_state():
    try:
        return json.loads(STATE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(st):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, indent=2) + "\n")


def summary(md):
    p = os.environ.get("GITHUB_STEP_SUMMARY")
    if p:
        with open(p, "a", encoding="utf-8") as f:
            f.write(md + "\n")


# ---------------------------------------------------------------- data
def all_issues():
    return [i for i in get(f"/repos/{REPO}/issues", state="all", per_page=100)
            if "pull_request" not in i]


class Tree:
    """Issue hierarchy from sub-issues, any depth (workstream -> task -> sub-task ...).

    Order is stable: top-level issues by number, children in the order shown on
    the parent issue (the sub-issues API order)."""

    def __init__(self, issues, children):
        self.by_num = {i["number"]: i for i in issues}
        self.children = {p: [c for c in cs if c in self.by_num] for p, cs in children.items()}
        self.parent = {c: p for p, cs in self.children.items() for c in cs}
        self._order, self._depth = {}, {}
        roots = sorted(n for n in self.by_num if n not in self.parent)
        stack = [(r, 0) for r in reversed(roots)]
        while stack:                                   # iterative pre-order DFS
            n, d = stack.pop()
            if n in self._order:
                continue                               # guard against cycles / duplicates
            self._order[n], self._depth[n] = len(self._order), d
            stack.extend((c, d + 1) for c in reversed(self.children.get(n, [])))

    def root(self, n):
        seen = set()
        while n in self.parent and n not in seen:
            seen.add(n)
            n = self.parent[n]
        return n

    def ancestors(self, n):
        out, seen = [], set()
        while n in self.parent and n not in seen:
            seen.add(n)
            n = self.parent[n]
            out.append(n)
        return out

    def order(self, n):
        return self._order.get(n, 10**9 + n)

    def depth(self, n):
        return self._depth.get(n, 0)

    def title(self, n):
        return self.by_num[n]["title"] if n in self.by_num else f"#{n}"


def parent_map(issues):
    """Build the full issue tree (kept under this name for the callers)."""
    children = {}
    for i in issues:
        if (i.get("sub_issues_summary") or {}).get("total"):
            try:
                cs = get(f"/repos/{REPO}/issues/{i['number']}/sub_issues", per_page=100)
                children[i["number"]] = [c["number"] for c in cs]
            except urllib.error.HTTPError as e:
                print(f"warn: sub_issues #{i['number']}: {e}", file=sys.stderr)
    return Tree(issues, children)


def activity(start, end, issues):
    """[(issue, [entry...])] for issues with comments/events in [start, end)."""
    res = []
    for i in issues:
        if ts(i["updated_at"]) < start:
            continue
        n, entries = i["number"], []
        created = ts(i["created_at"])
        if start <= created < end:
            entries.append({"t": created, "kind": "event", "ev": "opened", "text": "",
                            "who": (i.get("user") or {}).get("login"), "url": i["html_url"]})
        for c in get(f"/repos/{REPO}/issues/{n}/comments", since=iso(start), per_page=100):
            t = ts(c["created_at"])
            if start <= t < end:
                entries.append({"t": t, "kind": "comment", "text": c.get("body") or "",
                                "who": (c.get("user") or {}).get("login"), "url": c["html_url"]})
        for e in get(f"/repos/{REPO}/issues/{n}/events", per_page=100):
            if e.get("event") not in ("closed", "reopened"):
                continue
            t = ts(e["created_at"])
            if not (start <= t < end):
                continue
            entries.append({"t": t, "kind": "event", "ev": e["event"],
                            "reason": e.get("state_reason") if e["event"] == "closed" else None, "text": "",
                            "who": (e.get("actor") or {}).get("login"), "url": i["html_url"]})
        if entries:
            entries.sort(key=lambda x: x["t"])
            res.append((i, entries))
    return res


def group_key(issue, pm):
    """Top-level workstream of an issue: (number, title)."""
    r = pm.root(issue["number"])
    return r, pm.title(r)


# ---------------------------------------------------------------- log
def _entry_lines(es):
    out = []
    for e in es:
        who = f" · {e['who']}" if e["who"] else ""
        if e["kind"] == "comment":
            out.append(f"- **{nyf(e['t'])}**{who} · [{T('comment')}]({e['url']})")
            out.append(quote(e["text"]))
        else:
            out.append(f"- **{nyf(e['t'])}**{who} · {T('state')}: {ev_label(e)}")
    return out


def tree_list(items, pm, fmt=None):
    """Bullet list grouped by workstream, indented by depth in the issue tree.

    fmt(issue, indent) -> list of lines; default is '- #n title'. A workstream
    heading line is printed when the workstream itself is not in `items`."""
    fmt = fmt or (lambda i, ind: [f"{ind}- #{i['number']} {i['title']}"])
    groups = {}
    for i in items:
        groups.setdefault(pm.root(i["number"]), []).append(i)
    out = []
    for r in sorted(groups, key=pm.order):
        its = sorted(groups[r], key=lambda i: pm.order(i["number"]))
        listed = {x["number"] for x in its} | {r}       # the workstream line is always shown
        if its[0]["number"] != r:
            out.append(f"- **{pm.title(r)} #{r}**")    # workstream header (level 0)
        for i in its:
            n = i["number"]
            # nest under the nearest ancestor that is actually in this list, so an item
            # whose parent sits in another section is not drawn under an unrelated sibling
            level = 0 if n == r else sum(1 for a in pm.ancestors(n) if a in listed)
            lines = fmt(i, "  " * level)
            par = pm.parent.get(n)
            if par is not None and par != r and par not in listed:
                lines[0] += f"（← #{par}）"
            out.extend(lines)
        out.append("")
    return out


def render_groups(acts, pm, level=2):
    """Activity laid out along the issue tree: workstream -> sub-issue -> sub-sub-issue.

    Every active issue appears under its full chain of parents; parents without
    activity of their own are still shown as headings (marked idle) so the hierarchy
    stays visible. Order is fixed (issue number / sub-issue order), not by time."""
    active = {i["number"]: es for i, es in acts}
    if not active:
        return []
    need = set(active)
    for n in active:
        need.update(pm.ancestors(n))
    out = []

    def walk(n, d):
        h = "#" * min(level + d, 6)
        i = pm.by_num.get(n, {})
        state = i.get("state", "")
        idle = "" if n in active or d == 0 else T("idle_mark")
        if d == 0:
            out.append(f"{h} {pm.title(n)} #{n}" + (f" · {state}" if state == "closed" else "") + idle)
        else:
            out.append(f"{h} #{n} {pm.title(n)} · {state}{idle}")
        out.append("")
        if n in active:
            out.extend(_entry_lines(active[n]))
            out.append("")
        for c in pm.children.get(n, []):
            if c in need:
                walk(c, d + 1)

    for r in sorted({pm.root(n) for n in need}, key=pm.order):
        walk(r, 0)
    return out


def render_log(start, end, acts, issues, pm):
    n_comments = sum(1 for _, es in acts for e in es if e["kind"] == "comment")
    n_closed = sum(1 for _, es in acts if any(e.get("ev") == "closed" for e in es))
    n_new = sum(1 for _, es in acts if any(e.get("ev") == "opened" for e in es))
    out = [
        f"# {T('log_title', day=day_title(end))}",
        "",
        f"- {T('win_ny')}: {nyf(start, '%Y-%m-%d %H:%M')} → {nyf(end, '%Y-%m-%d %H:%M')}",
        f"- {T('win_utc')}: {iso(start)} → {iso(end)}",
        f"- {T('summary', n=len(acts), o=n_new, c=n_closed, m=n_comments)}",
        f"- {T('source')}",
        "",
    ]
    out += render_groups(acts, pm)

    active = {i["number"] for i, _ in acts}
    idle = [i for i in issues if i["state"] == "open" and i["number"] not in active]
    out += [f"## {T('idle_h')}", ""]
    out += tree_list(idle, pm) or [f"- {T('none')}", ""]
    return "\n".join(out)


def cmd_log(a):
    now = ts(a.end) if a.end else dt.datetime.now(UTC)
    today = now.astimezone(NY).date()
    path = ROOT / "logs" / f"{today:%Y}" / f"{today:%Y-%m-%d}.md"
    if not guard(a, now, path):
        return 0
    st = load_state()
    if a.start:
        start = ts(a.start)
    elif st.get("last_log_date") == today.isoformat() and st.get("last_log_start"):
        start = ts(st["last_log_start"])          # re-run today: regenerate same window
    elif st.get("last_log_end"):
        start = ts(st["last_log_end"])            # rolling: continue from last log
    else:
        start = dt.datetime.combine(today, dt.time(0), NY)
    issues = all_issues()
    md = render_log(start, now, activity(start, now, issues), issues, parent_map(issues))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(md, encoding="utf-8")
    if not (a.start or a.end):
        save_state({**st, "last_log_start": iso(start), "last_log_end": iso(now),
                    "last_log_date": today.isoformat()})
    summary(md)
    print(f"wrote {path}")
    return 0


# ---------------------------------------------------------------- week
def render_week(start, end, acts, issues, pm, charts_stamp=None):
    y, w, _ = end.astimezone(NY).isocalendar()
    n_comments = sum(1 for _, es in acts for e in es if e["kind"] == "comment")
    closed = [(i, e) for i, es in acts for e in es if e.get("ev") == "closed"]
    opened = [(i, e) for i, es in acts for e in es if e.get("ev") == "opened"]
    out = [
        f"# {T('week_title', wk=f'{y}-W{w:02d}', a=nyf(start, '%m-%d'), b=nyf(end, '%m-%d'))}",
        "",
        f"- {T('win_ny')}: {nyf(start, '%Y-%m-%d %H:%M')} → {nyf(end, '%Y-%m-%d %H:%M')}",
        f"- {T('summary', n=len(acts), o=len(opened), c=len(closed), m=n_comments)}",
        f"- {T('source')}",
        "",
    ]
    if charts_stamp:
        out += [f"## {T('charts')}", "",
                f"![{T('daily_alt')}](../../charts/daily-{charts_stamp}.png)", "",
                f"![{T('effort_alt')}](../../charts/effort-{charts_stamp}.png)", ""]

    # per-workstream table
    rows = {}
    for i, es in acts:
        k = group_key(i, pm)
        r = rows.setdefault(k, {"issues": set(), "c": 0, "new": 0, "closed": 0})
        r["issues"].add(i["number"])
        r["c"] += sum(1 for e in es if e["kind"] == "comment")
        r["new"] += sum(1 for e in es if e.get("ev") == "opened")
        r["closed"] += sum(1 for e in es if e.get("ev") == "closed")
    out += [f"## {T('table_h')}", "", T("table_cols"), "|---|---:|---:|---:|---|"]
    for (pn, pt), r in sorted(rows.items(), key=lambda kv: pm.order(kv[0][0])):
        refs = " ".join(f"#{n}" for n in sorted(r["issues"], key=pm.order))
        out.append(f"| {pt} #{pn} | {r['c']} | {r['new']} | {r['closed']} | {refs} |")
    if not rows:
        out.append(f"| {T('none')} | 0 | 0 | 0 | |")
    out.append("")

    sep = ", " if LANG == "en" else "，"
    lp, rp = (" (", ")") if LANG == "en" else ("（", "）")
    out += [f"## {T('closed_h')}", ""]
    out += [f"- #{i['number']} {i['title']}{lp}{nyf(e['t'], '%m-%d')}{sep}{e.get('reason') or T('closed')}{rp}" for i, e in closed] or [f"- {T('none')}"]
    out += ["", f"## {T('opened_h')}", ""]
    out += [f"- #{i['number']} {i['title']}{lp}{nyf(e['t'], '%m-%d')}{rp}" for i, e in opened] or [f"- {T('none')}"]
    out.append("")

    stale_cut = end - dt.timedelta(days=STALE_DAYS)
    stale = [i for i in issues if i["state"] == "open" and ts(i["updated_at"]) < stale_cut]
    out += [f"## {T('stale_h', d=STALE_DAYS)}", ""]
    out += tree_list(stale, pm, lambda i, ind: [
        f"{ind}- #{i['number']} {i['title']}{lp}{T('last_upd', d=nyf(ts(i['updated_at']), '%m-%d'))}{rp}"]) or [f"- {T('none')}", ""]
    out += [f"## {T('details_h')}", ""]
    out += render_groups(acts, pm, level=3) or [f"- {T('none')}", ""]
    return "\n".join(out)


def cmd_week(a):
    now = dt.datetime.now(UTC)
    local = now.astimezone(NY)
    y, w, _ = local.isocalendar()
    path = ROOT / "reports" / f"{y}" / f"{y}-W{w:02d}.md"
    if not a.force and local.weekday() != WEEK_DAY:
        print(f"skip: weekly report runs on weekday {WEEK_DAY} (0=Mon; today {local:%a})")
        return 0
    if not guard(a, now, path):
        return 0
    st = load_state()
    key = f"{y}-W{w:02d}"
    if a.start:
        start = ts(a.start)
    elif st.get("last_week_key") == key and st.get("last_week_start"):
        start = ts(st["last_week_start"])         # re-run this week: same window
    elif st.get("last_week_end"):
        start = ts(st["last_week_end"])           # rolling: continue from last report
    else:
        monday = local.date() - dt.timedelta(days=local.weekday())
        start = dt.datetime.combine(monday, dt.time(0), NY)
    issues = all_issues()
    pm = parent_map(issues)
    md = render_week(start, now, activity(start, now, issues), issues, pm,
                     charts_stamp=local.date().isoformat() if a.charts else None)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(md, encoding="utf-8")
    if not a.start:
        save_state({**st, "last_week_start": iso(start), "last_week_end": iso(now), "last_week_key": key})
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"wrote=true\npath={path}\n")
    summary(md)
    print(f"wrote {path}")
    return 0


# ---------------------------------------------------------------- plan
PROJECT_QUERY = """
query($login:String!, $num:Int!, $after:String) {
  repositoryOwner(login:$login) {
    ... on ProjectV2Owner {
      projectV2(number:$num) {
        items(first:100, after:$after) {
          pageInfo { hasNextPage endCursor }
          nodes {
            status:   fieldValueByName(name:"Status")   { ... on ProjectV2ItemFieldSingleSelectValue { name } }
            priority: fieldValueByName(name:"Priority") { ... on ProjectV2ItemFieldSingleSelectValue { name } }
            content { ... on Issue { number repository { nameWithOwner } } }
          }
        }
      }
    }
  }
}"""


def project_fields():
    """issue number -> (status, priority), or None if the project is not configured."""
    tok, num = os.environ.get("PROJECT_TOKEN"), os.environ.get("PROJECT_NUMBER")
    if not (tok and num):
        return None
    owner = os.environ.get("PROJECT_OWNER") or REPO.split("/")[0]
    out, after = {}, None
    while True:
        data, _ = _request(API + "/graphql", {"query": PROJECT_QUERY,
                           "variables": {"login": owner, "num": int(num), "after": after}}, token=tok)
        if data.get("errors"):
            raise RuntimeError(data["errors"])
        items = data["data"]["repositoryOwner"]["projectV2"]["items"]
        for node in items["nodes"]:
            c = node.get("content") or {}
            if (c.get("repository") or {}).get("nameWithOwner", "").lower() == REPO.lower():
                out[c["number"]] = ((node.get("status") or {}).get("name"),
                                    (node.get("priority") or {}).get("name"))
        if not items["pageInfo"]["hasNextPage"]:
            return out
        after = items["pageInfo"]["endCursor"]


def bucket(status):
    s = (status or "").strip().lower()
    for key, names in (("prog", ("in progress", "doing")), ("todo", ("todo", "to do")),
                       ("block", ("blocked",)), ("backlog", ("backlog",)), ("done", ("done",))):
        if s in names:
            return key
    return "other"


_COMMENTS = {}


def comments_of(n):
    if n not in _COMMENTS:
        _COMMENTS[n] = get(f"/repos/{REPO}/issues/{n}/comments", per_page=100)
    return _COMMENTS[n]


def last_comment(n):
    cs = comments_of(n)
    return cs[-1] if cs else None


def plan_buckets(issues, fields):
    """{'prog'|'todo'|'block'|'backlog'|'done'|'other'|'all': [open issue...]}"""
    open_issues = [i for i in issues if i["state"] == "open"]
    if fields is None:
        return {"all": open_issues}
    by = {}
    for i in open_issues:
        by.setdefault(bucket(fields.get(i["number"], (None, None))[0]), []).append(i)
    return by


def render_plan(now, by, pm, fields, overnight, since):
    out = [f"# 今日计划 {day_title(now)}", "",
           f"- 生成：纽约 {nyf(now, '%H:%M')}；状态来源："
           + ("Project「Status」字段" if fields is not None else "未配置 Project，按最近更新排序"), ""]

    def line(i, ind):
        pri = (fields or {}).get(i["number"], (None, None))[1]
        lines = [f"{ind}- [ ] #{i['number']} {i['title']}" + (f"（{pri}）" if pri else "")]
        c = last_comment(i["number"])
        if c:
            lines.append(f"{ind}  - 上次记录 {nyf(ts(c['created_at']))}：{snippet(c.get('body'))} [→]({c['html_url']})")
        return lines

    def section(title, items):
        out.extend([f"## {title}", ""])
        out.extend(tree_list(items, pm, line) or ["- （无）", ""])

    stale_cut = now - dt.timedelta(days=STALE_DAYS)
    if fields is not None:
        section("进行中（In Progress）", by.get("prog", []))
        section("待办（Todo）", by.get("todo", []))
        section("阻塞（Blocked）", by.get("block", []))
        stale = [i for i in by.get("prog", []) + by.get("todo", []) if ts(i["updated_at"]) < stale_cut]
        backlog_n, other_n = len(by.get("backlog", [])), len(by.get("other", []))
    else:
        section("Open issue（按最近更新）", by.get("all", []))
        stale = [i for i in by.get("all", []) if ts(i["updated_at"]) < stale_cut]
        backlog_n = other_n = 0

    out += [f"## 上次日志之后的新动态（纽约 {nyf(since, '%m-%d %H:%M')} 起）", ""]
    if overnight:
        for i, es in sorted(overnight, key=lambda x: pm.order(x[0]["number"])):
            for e in es:
                what = snippet(e["text"], 100) if e["kind"] == "comment" else f"{T('state')}: {ev_label(e)}"
                out.append(f"- #{i['number']} {nyf(e['t'])} · {what} [→]({e['url']})")
    else:
        out.append("- （无）")
    out.append("")

    out += [f"## 超过 {STALE_DAYS} 天无更新（进行中 / 待办）", ""]
    out += tree_list(stale, pm, lambda i, ind: [
        f"{ind}- #{i['number']} {i['title']}（最后更新 {nyf(ts(i['updated_at']), '%m-%d')}）"]) or ["- （无）", ""]
    if backlog_n or other_n:
        out += ["## 备注", "", f"- Backlog {backlog_n} 项、未设状态 {other_n} 项未列出", ""]
    return "\n".join(out)


def cmd_plan(a):
    now = dt.datetime.now(UTC)
    today = now.astimezone(NY).date()
    path = ROOT / "plans" / f"{today:%Y}" / f"{today:%Y-%m-%d}.md"
    if not guard(a, now, path):
        return 0
    st = load_state()
    since = ts(st["last_log_end"]) if st.get("last_log_end") else now - dt.timedelta(hours=14)
    issues = all_issues()
    try:
        fields = project_fields()
    except Exception as e:  # keep producing a plan even if the project query fails
        print(f"warn: project query failed, falling back: {e}", file=sys.stderr)
        fields = None
    pm = parent_map(issues)
    by = plan_buckets(issues, fields)
    overnight = activity(since, now, issues)
    md = render_plan(now, by, pm, fields, overnight, since)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(md, encoding="utf-8")
    summary(md)
    print(f"wrote {path}")
    return 0


# ---------------------------------------------------------------- boards
# Board IDs are written many ways in comments; map them to one canonical form so a
# board's whole history can be found and counted. Comment text itself is never changed.
#   BNL_FEMB_IO-1865-1K_00024 | 1865-1k-35 | 1826-1L-0051 | 00056_BNL_FEMB_I0-1865-1J
#     -> 1865-1K-00024        | 1865-1K-00035 | 1826-1L-00051 | 1865-1J-00056
BOARD_RE = re.compile(r"(?<![0-9A-Za-z])(\d{4})[-_ ](\d[A-Za-z])[-_ ](\d{1,5})(?![0-9])")
BOARD_REV_RE = re.compile(r"(?<![0-9])(\d{5})_BNL_FEMB_I[O0]-(\d{4})-(\d[A-Za-z])", re.I)
# Short forms that cannot be resolved safely (K39, board_12 ...). Listed for review only,
# unless mapped explicitly in boards/aliases.json, e.g. {"K39": "1865-1K-00039"}.
# aliases.json can also correct a mistyped full ID: {"1826-1K-00051": "1826-1L-00051"}.
SHORT_RE = re.compile(r"(?<![0-9A-Za-z])(?:[KL]\d{2,3}|board[ _]?\d{1,3})(?![0-9A-Za-z])", re.I)


def canon(series, batch, serial):
    return f"{series}-{batch.upper()}-{int(serial):05d}"


def board_refs(text, aliases):
    """[(canonical id, raw spelling)] found in text, in order, de-duplicated."""
    out, seen = [], set()
    for m in BOARD_REV_RE.finditer(text):
        out.append((canon(m.group(2), m.group(3), m.group(1)), m.group(0)))
    for m in BOARD_RE.finditer(text):
        out.append((canon(*m.groups()), m.group(0)))
    for m in SHORT_RE.finditer(text):
        raw = m.group(0)
        key = next((k for k in aliases if k.lower() == raw.lower()), None)
        if key:
            out.append((aliases[key], raw))
    res = []
    for cid, raw in out:
        cid = aliases.get(cid, cid)             # aliases can also fix typos in full IDs
        if cid not in seen:
            seen.add(cid)
            res.append((cid, raw))
    return res


def cmd_boards(a):
    """boards/index.md + boards/boards.json: every board ID with all mentions, newest first."""
    out_dir = ROOT / "boards"
    try:
        aliases = json.loads((out_dir / "aliases.json").read_text(encoding="utf-8"))
        aliases = {k: v for k, v in aliases.items() if not k.startswith("_")}
    except (FileNotFoundError, json.JSONDecodeError):
        aliases = {}
    issues = {i["number"]: i for i in all_issues()}
    sources = []                                   # (time, issue#, url, text, kind)
    for i in issues.values():
        sources.append((ts(i["created_at"]), i["number"], i["html_url"], i["title"] + "\n" + (i.get("body") or ""), "issue"))
    for c in get(f"/repos/{REPO}/issues/comments", per_page=100):
        n = int(c["issue_url"].rsplit("/", 1)[1])
        if n in issues:
            sources.append((ts(c["created_at"]), n, c["html_url"], c.get("body") or "", "comment"))
    boards, short = {}, []
    for t, n, url, text, kind in sources:
        refs = board_refs(text, aliases)
        for cid, raw in refs:
            b = boards.setdefault(cid, {"mentions": [], "spellings": set()})
            b["spellings"].add(raw)
            b["mentions"].append({"time": iso(t), "issue": n, "title": issues[n]["title"], "url": url,
                                  "kind": kind, "text": snippet(text, 160)})
        mapped = {k.lower() for k in aliases}
        for raw in dict.fromkeys(m.group(0) for m in SHORT_RE.finditer(text)):
            if raw.lower() not in mapped:
                short.append((t, n, url, raw, snippet(text, 120)))
    for b in boards.values():
        b["mentions"].sort(key=lambda m: m["time"], reverse=True)

    def series_key(cid):
        s, b_, n = cid.split("-")
        return (s, b_, int(n))

    lines = ["# Board index", "",
             "Generated by `worklog.py boards` from all issue titles, bodies and comments. "
             "Board IDs are normalized to `SERIES-BATCH-SERIAL` (e.g. `1865-1K-00039`); "
             "the original text is unchanged. Short forms like `K39` are only counted if mapped in "
             "[aliases.json](aliases.json).", "",
             f"**{len(boards)} boards**", "",
             "| Board | Mentions | First | Last | Issues | Spellings seen |",
             "|---|---:|---|---|---|---|"]
    for cid in sorted(boards, key=series_key):
        b = boards[cid]
        ms = b["mentions"]
        iss = " ".join(f"#{x}" for x in sorted({m["issue"] for m in ms}))
        sp = ", ".join(f"`{x}`" for x in sorted(b["spellings"]))
        lines.append(f"| [{cid}](#{cid.lower()}) | {len(ms)} | {nyf(ts(ms[-1]['time']), '%m-%d')} | "
                     f"{nyf(ts(ms[0]['time']), '%m-%d')} | {iss} | {sp} |")
    lines.append("")
    for cid in sorted(boards, key=series_key):
        lines += [f"## {cid}", ""]
        for m in boards[cid]["mentions"]:
            lines.append(f"- **{nyf(ts(m['time']), '%Y-%m-%d %H:%M')}** · #{m['issue']} {m['title']} · "
                         f"[{m['kind']}]({m['url']}): {m['text']}")
        lines.append("")
    if short:
        lines += ["## Unresolved short references", "",
                  "Not counted above. Add an entry to `aliases.json` (e.g. `\"K39\": \"1865-1K-00039\"`) to include one.", ""]
        for t, n, url, raw, text in sorted(short, reverse=True):
            lines.append(f"- `{raw}` · {nyf(t, '%Y-%m-%d')} · #{n} · [link]({url}): {text}")
        lines.append("")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "index.md").write_text("\n".join(lines), encoding="utf-8")
    data = {cid: {"spellings": sorted(b["spellings"]), "mentions": b["mentions"]}
            for cid, b in sorted(boards.items(), key=lambda kv: series_key(kv[0]))}
    (out_dir / "boards.json").write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    if not (out_dir / "aliases.json").exists():
        (out_dir / "aliases.json").write_text(json.dumps(
            {"_comment": "Map short refs or mistyped IDs to canonical IDs, e.g. \"K39\": \"1865-1K-00039\", \"1826-1K-00051\": \"1826-1L-00051\""},
            indent=1) + "\n", encoding="utf-8")
    print(f"boards: {len(boards)} ids, {sum(len(b['mentions']) for b in boards.values())} mentions, "
          f"{len(short)} unresolved short refs")
    return 0


# ---------------------------------------------------------------- main
def guard(a, now, path):
    """Actions fires at two UTC hours to cover EDT/EST; run only the right one."""
    if a.force:
        return True
    local = now.astimezone(NY)
    if local.weekday() >= 5 and not ALL_DAYS:
        print(f"skip: weekend ({local:%a})")
        return False
    if local.hour < a.at_hour:
        print(f"skip: {local:%H:%M} NY is before {a.at_hour}:00")
        return False
    if path.exists():
        print(f"skip: {path} already exists")
        return False
    return True


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    lg = sub.add_parser("log")
    lg.add_argument("--at-hour", type=int, default=18)
    lg.add_argument("--start", help="override window start (ISO, e.g. 2026-09-29T04:00:00Z)")
    lg.add_argument("--end", help="override window end (ISO)")
    lg.add_argument("--force", action="store_true")
    pl = sub.add_parser("plan")
    pl.add_argument("--at-hour", type=int, default=8)
    pl.add_argument("--force", action="store_true")
    wk = sub.add_parser("week")
    wk.add_argument("--at-hour", type=int, default=18)
    wk.add_argument("--start", help="override window start (ISO)")
    wk.add_argument("--charts", action="store_true", help="embed charts/daily|effort-<date>.png")
    wk.add_argument("--force", action="store_true")
    sub.add_parser("boards")
    a = p.parse_args(argv)
    if not TOKEN:
        print("warn: no GITHUB_TOKEN; unauthenticated rate limit is 60 req/h", file=sys.stderr)
    return {"log": cmd_log, "plan": cmd_plan, "week": cmd_week, "boards": cmd_boards}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
