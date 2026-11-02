"""
work-log 周报图表（只画图，不调用 AI；周报正文由 worklog.py week 生成）

每周运行一次：
  1. 拉取仓库所有 issue、sub-issue 父子关系、最近 N 周的 comment
  2. sub-issue 的 comment 归到它的顶层工作线（如 #14 shifter -> [FEMB] #11）
  3. 画出最近 N 周各工作线的日志条数堆叠柱状图 -> charts/effort-<日期>.png、effort-latest.png
  4. 画出最近 N 天的每日活跃度 -> charts/daily-<日期>.png、daily-latest.png

环境变量：
  REPO      owner/repo（Actions 里自动传入）
  GH_TOKEN  GitHub token（Actions 里用 github.token）
  WEEKS     每周图显示最近几周，默认 8（开头没有数据的周会被省略）
  DAILY_DAYS 每日图显示最近几天，默认 14
  LANG      输出语言：en（默认）或 zh
  DEMO=1    不联网，用样例数据测试出图
"""
import collections, datetime as dt, json, os, random, re, urllib.request
from zoneinfo import ZoneInfo

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.ticker import MaxNLocator

TZ = ZoneInfo("America/New_York")
REPO = os.environ.get("REPO", "lkebnl/llm-learning")
TOKEN = os.environ.get("GH_TOKEN", "")
WEEKS = int(os.environ.get("WEEKS", "8"))
DAILY_DAYS = int(os.environ.get("DAILY_DAYS", "14"))
DEMO = os.environ.get("DEMO") == "1"
LANG = os.environ.get("REPORT_LANG", os.environ.get("LANG_OUT", "en")).lower()
LANG = "zh" if LANG.startswith("zh") else "en"

T = {
    "en": {
        "ylabel": "Log entries", "xlabel": "Week (starting Monday)",
        "title": "Weekly effort by workstream · {repo} (sub-issues rolled up)",
        "title_day": "Daily log entries · last {n} days · {repo}",
        "xlabel_day": "Day", "h_day": "Daily activity", "wd": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
        "h_chart": "Weekly effort distribution", "alt": "Weekly effort distribution",
        "col": ("Workstream", "This week", "Last {n} weeks"),
        "raw": "This week's raw log", "none": "none",
        "empty": "_No new log entries this week._", "demo": "_(Demo mode, model not called.)_",
        "fail": "_Summary generation failed: {e}_",
        "noai": "_Charts and this week's raw log below. For a written summary, paste the raw log into your AI assistant._",
        "prompt": ("Below are my daily progress notes from this week, grouped by workstream "
                   "(sub-issues are rolled up into their parent workstream). Some notes are in Chinese. "
                   "Write a concise weekly report in English for my supervisor: for each workstream give "
                   "2-4 bullet points covering what was done, results/data, and next steps. "
                   "Do not invent anything not in the notes; keep board IDs, serial numbers and other "
                   "identifiers exactly as written. End with a short list of cross-workstream risks or open items.\n\n"),
    },
    "zh": {
        "ylabel": "日志条数", "xlabel": "周（周一起）",
        "title": "每周精力分布 · {repo} · 按工作线（sub-issue 归入父级）",
        "title_day": "每日日志 · 最近 {n} 天 · {repo}",
        "xlabel_day": "日期", "h_day": "每日活跃度", "wd": ["一", "二", "三", "四", "五", "六", "日"],
        "h_chart": "每周精力分布", "alt": "每周精力分布",
        "col": ("工作线", "本周条数", "近 {n} 周合计"),
        "raw": "本周原始日志", "none": "无",
        "empty": "_本周没有新的日志。_", "demo": "_（测试模式，未调用模型）_",
        "fail": "_周报生成失败：{e}_",
        "noai": "_图表和本周原始日志见下方；需要文字周报时，把原始日志贴给 AI 整理。_",
        "prompt": ("下面是我这一周在各工作线下记录的每日进展（sub-issue 已归入所属工作线）。"
                   "请整理成简洁的中文周报：每条工作线 2-4 个要点，写清完成了什么、结果/数据、下一步。"
                   "不要编造日志中没有的内容，保留板号、编号等原始信息。最后列出跨工作线的风险或待办。\n\n"),
    },
}[LANG]

# 固定顺序的分类色（按工作线 issue 编号排序后依次分配，颜色跟着工作线走）
PALETTE = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]


# ---------- GitHub API ----------
def api(path):
    url = path if path.startswith("http") else f"https://api.github.com/{path}"
    out = []
    while url:
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        with urllib.request.urlopen(req) as r:
            data = json.load(r)
            link = r.headers.get("Link", "")
        if isinstance(data, dict):
            return data
        out += data
        m = re.search(r'<([^>]+)>;\s*rel="next"', link)
        url = m.group(1) if m else None
    return out


def short_label(title):
    m = re.match(r"\s*\[([^\]]+)\]", title)
    s = m.group(1) if m else title
    return s if len(s) <= 22 else s[:21] + "…"


def load_real(since_utc):
    issues = [i for i in api(f"repos/{REPO}/issues?state=all&per_page=100") if "pull_request" not in i]
    by_num = {i["number"]: i for i in issues}
    parent = {}
    for i in issues:
        if (i.get("sub_issues_summary") or {}).get("total"):
            for c in api(f"repos/{REPO}/issues/{i['number']}/sub_issues?per_page=100"):
                parent[c["number"]] = i["number"]
    stamp = since_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    comments = []
    for c in api(f"repos/{REPO}/issues/comments?since={stamp}&per_page=100"):
        created = dt.datetime.fromisoformat(c["created_at"].replace("Z", "+00:00"))
        if created < since_utc:          # since 按 updated_at 过滤，这里再按创建时间筛
            continue
        num = int(c["issue_url"].rsplit("/", 1)[1])
        if num in by_num:
            comments.append({"issue": num, "created": created, "body": c["body"].strip()})
    return by_num, parent, comments


def load_demo(since_utc):
    titles = {6: "[WIB] 本周核心工作", 7: "[ISEG / WIEC] 本周核心工作", 8: "[LN2 Dewar Refill]",
              11: "[FEMB] 本周核心工作", 12: "[Cable] 本周核心工作", 13: "CRP Patch Panel",
              14: "shifter for HD / VD FEMB 09232026", 15: "paper for FEMB QC"}
    by_num = {n: {"number": n, "title": t} for n, t in titles.items()}
    parent = {14: 11, 15: 11}
    rnd = random.Random(7)
    weight = {6: 1.2, 7: 2, 8: 1, 11: 3, 12: 1, 13: 1.6, 14: 1, 15: .9}
    comments, t = [], since_utc
    now = dt.datetime.now(dt.timezone.utc)
    while t < now:
        if t.astimezone(TZ).weekday() < 5:
            for n, w in weight.items():
                for _ in range(int(rnd.random() * w * 1.3)):
                    comments.append({"issue": n, "created": t + dt.timedelta(hours=rnd.randint(9, 18)),
                                     "body": f"示例日志 #{n}"})
        t += dt.timedelta(days=1)
    return by_num, parent, comments


# ---------- 聚合 ----------
def top_level(num, parent):
    seen = set()
    while num in parent and num not in seen:
        seen.add(num)
        num = parent[num]
    return num


def week_start(d):
    local = d.astimezone(TZ).date()
    return local - dt.timedelta(days=local.weekday())


# ---------- 出图 ----------
def pick_font():
    names = {f.name for f in font_manager.fontManager.ttflist}
    for n in ["Noto Sans CJK SC", "Noto Sans CJK JP", "Noto Sans SC", "WenQuanYi Zen Hei", "Source Han Sans SC"]:
        if n in names:
            return n
    return "DejaVu Sans"


def draw(periods, ticks, streams, colors, counts, labels, title, xlabel, path, muted=()):
    plt.rcParams.update({"font.family": pick_font(), "font.size": 10, "axes.unicode_minus": False})
    fig, ax = plt.subplots(figsize=(max(6.5, 3.2 + 0.52 * len(periods)), 4.6), dpi=160)
    fig.patch.set_facecolor("#fcfcfb"); ax.set_facecolor("#fcfcfb")
    x = list(range(len(periods)))
    bottom = [0] * len(periods)
    for s in streams:
        vals = [counts[p].get(s, 0) for p in periods]
        ax.bar(x, vals, width=0.62, bottom=bottom, color=colors[s],
               edgecolor="#fcfcfb", linewidth=1.5, label=labels[s], zorder=3)
        bottom = [b + v for b, v in zip(bottom, vals)]
    top = max(bottom) if any(bottom) else 1
    for i, total in enumerate(bottom):
        if total:
            ax.text(i, total + top * 0.015, str(total), ha="center", va="bottom", fontsize=9, color="#0b0b0b")
    ax.set_ylim(0, top * 1.12)
    ax.yaxis.set_major_locator(MaxNLocator(integer=True))      # 条数只显示整数刻度
    ax.set_xticks(x, ticks)
    ax.set_xlim(-0.6, max(len(periods), 6) - 0.4)                # 数据少时柱子不会被拉得很宽
    ax.set_ylabel(T["ylabel"], color="#52514e")
    ax.set_title(title, loc="left", fontsize=12, color="#0b0b0b", pad=12)
    ax.grid(axis="y", color="#e1e0d9", linewidth=0.8, zorder=0)
    for side in ["top", "right", "left"]:
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color("#c3c2b7")
    ax.tick_params(axis="both", length=0, colors="#52514e")
    for i, lab in enumerate(ax.get_xticklabels()):
        lab.set_color("#b5b3ac" if i in muted else "#52514e")   # 周末日期淡色
    ax.set_xlabel(xlabel, color="#898781")
    if streams:
        ax.legend(loc="upper left", bbox_to_anchor=(1.0, 1.0), frameon=False, fontsize=9)
    fig.tight_layout()
    fig.savefig(path, facecolor=fig.get_facecolor())
    plt.close(fig)


def main():
    now = dt.datetime.now(dt.timezone.utc)
    this_monday = week_start(now)
    first_monday = this_monday - dt.timedelta(weeks=WEEKS - 1)
    since = dt.datetime.combine(first_monday, dt.time(), TZ).astimezone(dt.timezone.utc)

    by_num, parent, comments = (load_demo if DEMO else load_real)(since)

    # 图表数据
    weeks = [first_monday + dt.timedelta(weeks=i) for i in range(WEEKS)]
    counts = {w: collections.Counter() for w in weeks}
    today = now.astimezone(TZ).date()
    days = [today - dt.timedelta(days=DAILY_DAYS - 1 - i) for i in range(DAILY_DAYS)]
    dcounts = {d: collections.Counter() for d in days}
    for c in comments:
        top = top_level(c["issue"], parent)
        counts[week_start(c["created"])][top] += 1
        d = c["created"].astimezone(TZ).date()
        if d in dcounts:
            dcounts[d][top] += 1
    streams = sorted({s for w in weeks for s in counts[w]})
    labels = {s: f"{short_label(by_num[s]['title'])} #{s}" for s in streams}
    colors = {s: PALETTE[k % len(PALETTE)] for k, s in enumerate(streams)}   # 两张图同一工作线同色

    # 每周图：省略开头没有数据的周（仓库刚建时不会出现一排空柱）
    shown = weeks
    while len(shown) > 1 and not counts[shown[0]]:
        shown = shown[1:]

    os.makedirs("charts", exist_ok=True)
    stamp = today.isoformat()
    for name in ("effort-latest.png", f"effort-{stamp}.png"):
        draw(shown, [f"{w.month}/{w.day}" for w in shown], streams, colors, counts, labels,
             T["title"].format(repo=REPO), T["xlabel"], f"charts/{name}")
    dticks = [f"{d.month}/{d.day}\n{T['wd'][d.weekday()]}" for d in days]
    weekend = {i for i, d in enumerate(days) if d.weekday() >= 5}
    dstreams = [s for s in streams if any(dcounts[d].get(s) for d in days)]
    for name in ("daily-latest.png", f"daily-{stamp}.png"):
        draw(days, dticks, dstreams, colors, dcounts, labels,
             T["title_day"].format(n=DAILY_DAYS, repo=REPO), T["xlabel_day"], f"charts/{name}", muted=weekend)

    print(f"weeks={len(weeks)} streams={len(streams)} comments={len(comments)} this_week={sum(counts[this_monday].values())}")


if __name__ == "__main__":
    main()
