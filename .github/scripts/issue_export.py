#!/usr/bin/env python3
"""Export an issue and ALL nested sub-issues (with comments and images) to Markdown + PDF (one page per issue).

Usage:
    GITHUB_TOKEN=... REPO=owner/name python3 .github/scripts/issue_export.py 110 [--out exports] [--no-pdf] [--single-page]

Output: <out>/issue-<N>/issue-<N>.md, issue-<N>.html, issue-<N>.pdf, images/

Images in issues live behind signed URLs (private-user-images.githubusercontent.com, ~5 min validity),
so they are downloaded right after the API call and referenced locally from md/html/pdf.
"""
import argparse, html, json, os, re, sys, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

TOKEN = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
REPO = os.environ.get("REPO") or os.environ.get("GITHUB_REPOSITORY")
API = "https://api.github.com"
NY = timezone(timedelta(hours=-4))  # display only; close enough for a log export
try:
    from zoneinfo import ZoneInfo
    NY = ZoneInfo("America/New_York")
except Exception:
    pass


def req(url, accept="application/vnd.github.full+json"):
    r = urllib.request.Request(url, headers={
        "Accept": accept, "X-GitHub-Api-Version": "2022-11-28",
        **({"Authorization": f"Bearer {TOKEN}"} if TOKEN else {})})
    with urllib.request.urlopen(r, timeout=60) as resp:
        return resp.read(), resp.headers


def api(path):
    """GET with pagination; returns dict or concatenated list."""
    url = path if path.startswith("http") else f"{API}/repos/{REPO}/{path}"
    out = None
    while url:
        body, headers = req(url)
        data = json.loads(body)
        if not isinstance(data, list):
            return data
        out = (out or []) + data
        m = re.search(r'<([^>]+)>;\s*rel="next"', headers.get("Link", ""))
        url = m.group(1) if m else None
    return out or []


def fetch(n, seen):
    if n in seen:  # guard against cycles
        return None
    seen.add(n)
    it = api(f"issues/{n}")
    it["_comments"] = api(f"issues/{n}/comments?per_page=100") if it.get("comments") else []
    subs = api(f"issues/{n}/sub_issues?per_page=100") if (it.get("sub_issues_summary") or {}).get("total") else []
    it["_subs"] = [x for x in (fetch(s["number"], seen) for s in subs) if x]
    print(f"  #{n} {it['title']} ({len(it['_comments'])} comments, {len(it['_subs'])} sub-issues)", file=sys.stderr)
    return it


def walk(it, depth=0):
    yield it, depth
    for s in it["_subs"]:
        yield from walk(s, depth + 1)


def t(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(NY).strftime("%Y-%m-%d %H:%M")


# ---------- images ----------
UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


def collect_images(root, imgdir):
    """Download every <img> in rendered HTML. Returns {uuid_or_url: local_relpath}."""
    srcs = {}
    for it, _ in walk(root):
        for h in [it.get("body_html") or ""] + [c.get("body_html") or "" for c in it["_comments"]]:
            for src in re.findall(r'<img[^>]+src="([^"]+)"', h):
                src = html.unescape(src)
                if src.startswith("data:"):
                    continue
                m = re.search(UUID, src)
                srcs[m.group(0) if m else src] = src
    os.makedirs(imgdir, exist_ok=True)

    def dl(item):
        key, src = item
        try:
            data, headers = req(src, accept="*/*")
        except Exception as e:
            print(f"  ! image failed: {src[:90]} ({e})", file=sys.stderr)
            return key, None
        ct = headers.get("Content-Type", "")
        ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp",
               "image/svg+xml": ".svg"}.get(ct.split(";")[0], os.path.splitext(src.split("?")[0])[1] or ".img")
        name = (key if re.fullmatch(UUID, key) else str(abs(hash(key)))) + ext
        with open(os.path.join(imgdir, name), "wb") as f:
            f.write(data)
        return key, f"images/{name}"

    with ThreadPoolExecutor(8) as ex:
        res = dict(ex.map(dl, srcs.items()))
    print(f"  images: {sum(1 for v in res.values() if v)}/{len(res)} downloaded", file=sys.stderr)
    return res


def local_src(src, imgmap):
    m = re.search(UUID, src)
    return imgmap.get(m.group(0) if m else src)


def fix_html(h, imgmap):
    def rep(m):
        tag = m.group(0)
        src = html.unescape(re.search(r'src="([^"]+)"', tag).group(1))
        loc = local_src(src, imgmap)
        tag = re.sub(r'\s(width|height)="\d+"', "", tag)  # let CSS size it
        return tag.replace(re.search(r'src="([^"]+)"', tag).group(0), f'src="{loc}"') if loc else tag
    h = re.sub(r"<img[^>]*>", rep, h)
    # unwrap image links pointing to the signed URL so the PDF doesn't carry expiring links
    return re.sub(r'<a[^>]+href="https://private-user-images[^"]*"[^>]*>\s*(<img[^>]*>)\s*</a>', r"\1", h)


def fix_md(md, imgmap):
    def rep(m):
        loc = imgmap.get(m.group(1))
        return loc if loc else m.group(0)
    return re.sub(r"https://github\.com/user-attachments/assets/(" + UUID + ")", rep, md or "")


# ---------- markdown ----------
def to_md(root, imgmap):
    out = [f"# {root['title']} (#{root['number']})\n",
           f"> Exported {datetime.now(NY):%Y-%m-%d %H:%M} from {root['html_url']}\n", "## Contents\n"]
    for it, d in walk(root):
        mark = "x" if it["state"] == "closed" else " "
        out.append(f"{'  ' * d}- [{mark}] [#{it['number']} {it['title']}](#issue-{it['number']})")
    out.append("")
    for it, d in walk(root):
        s = it.get("sub_issues_summary") or {}
        out.append(f"\n{'#' * min(d + 2, 6)} <a id=\"issue-{it['number']}\"></a>#{it['number']} {it['title']}\n")
        meta = f"**{it['state']}** · {it['user']['login']} opened {t(it['created_at'])}"
        if it.get("closed_at"):
            meta += f" · closed {t(it['closed_at'])}"
        if s.get("total"):
            meta += f" · sub-issues {s['completed']}/{s['total']}"
        out.append(meta + f" · [link]({it['html_url']})\n")
        if it.get("body"):
            out.append(fix_md(it["body"], imgmap).strip() + "\n")
        for c in it["_comments"]:
            out.append(f"---\n**{c['user']['login']}** · {t(c['created_at'])}\n")
            out.append(fix_md(c["body"], imgmap).strip() + "\n")
    return "\n".join(out) + "\n"


# ---------- html / pdf ----------
CSS = """body{font-family:-apple-system,"Segoe UI","Noto Sans CJK SC","Microsoft YaHei",Helvetica,Arial,sans-serif;font-size:14px;color:#1f2328;background:#fff;margin:0}
.w{max-width:900px;margin:0 auto;padding:32px 24px}
h1{font-size:26px;font-weight:400;margin:0 0 8px}h2{font-size:20px;margin:0 0 6px}h3,h4,h5,h6{font-size:15px;margin:0 0 4px}.num{color:#59636e;font-weight:300}
section.d0>.meta{border-bottom:1px solid #d1d9e0;padding-bottom:14px}
section.d1{margin-top:36px;padding-top:20px;border-top:2px solid #1f2328}
section.d2,section.d3,section.d4{border-left:3px solid #d1d9e0;padding-left:16px;margin:22px 0}
.meta{color:#59636e;margin-bottom:10px}
.st{display:inline-block;color:#fff;border-radius:2em;padding:2px 9px;font-size:12px;font-weight:600;margin-right:6px}
.st.open{background:#1f883d}.st.closed{background:#8250df}
.c{border:1px solid #d1d9e0;border-radius:6px;margin:10px 0;overflow:hidden}
.ch{background:#f6f8fa;border-bottom:1px solid #d1d9e0;padding:6px 14px;color:#59636e}.ch b{color:#1f2328}
.cb{padding:10px 14px;overflow-wrap:anywhere}.cb>*:first-child{margin-top:0}.cb>*:last-child{margin-bottom:0}
.cb img{max-width:100%;max-height:520px;height:auto;display:block;margin:6px 0;border:1px solid #eaeef2}
.cb table{border-collapse:collapse;margin:6px 0}.cb td,.cb th{border:1px solid #d1d9e0;padding:4px 10px}.cb th{background:#f6f8fa}
.cb pre{background:#f6f8fa;padding:10px;border-radius:6px;white-space:pre-wrap}.cb code{font-family:ui-monospace,Consolas,monospace;font-size:12.5px}
.cb ul,.cb ol{padding-left:22px}
a{color:#0969da;text-decoration:none}
.toc{border:1px solid #d1d9e0;border-radius:6px;padding:12px 16px;margin:16px 0}.toc ul{list-style:none;padding:0;margin:8px 0 0}.toc li{margin:2px 0}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}.dot.open{background:#1f883d}.dot.closed{background:#8250df}
.foot{color:#8c959f;font-size:12px;margin-top:28px}"""


def to_html(root, imgmap):
    def comment(c):
        return (f'<div class="c"><div class="ch"><b>{c["user"]["login"]}</b> · {t(c["created_at"])}</div>'
                f'<div class="cb">{fix_html(c.get("body_html") or "", imgmap)}</div></div>')

    def block(it, d):
        n, st, s = it["number"], it["state"], it.get("sub_issues_summary") or {}
        lv = min(1 + d, 6)
        o = (f'<section id="i{n}" class="d{d}"><h{lv}>{html.escape(it["title"])} <span class="num">#{n}</span></h{lv}>'
             f'<div class="meta"><span class="st {st}">{st}</span>{it["user"]["login"]} opened {t(it["created_at"])}')
        if it.get("closed_at"):
            o += f" · closed {t(it['closed_at'])}"
        o += f" · {len(it['_comments'])} comment(s)"
        if s.get("total"):
            o += f" · sub-issues {s['completed']}/{s['total']}"
        o += "</div>"
        if it.get("body_html"):
            o += f'<div class="c"><div class="cb">{fix_html(it["body_html"], imgmap)}</div></div>'
        o += "".join(comment(c) for c in it["_comments"])
        if d == 0 and it["_subs"]:
            o += toc
        o += "".join(block(s, d + 1) for s in it["_subs"])
        return o + "</section>"

    toc = '<div class="toc"><b>Contents</b><ul>' + "".join(
        f'<li style="margin-left:{d * 18}px"><span class="dot {it["state"]}"></span>'
        f'<a href="#i{it["number"]}">#{it["number"]} {html.escape(it["title"])}</a></li>'
        for it, d in walk(root) if d > 0) + "</ul></div>"
    n = sum(1 for _ in walk(root))
    return (f'<!doctype html><html><head><meta charset="utf-8"><title>#{root["number"]} {html.escape(root["title"])}</title>'
            f'<style>{CSS}</style></head><body><div class="w">{block(root, 0)}'
            f'<div class="foot">Exported {datetime.now(NY):%Y-%m-%d %H:%M} from '
            f'<a href="{root["html_url"]}">{root["html_url"]}</a> · {n} issues</div></div></body></html>')


def to_pdf_single(html_path, pdf_path, width=950):
    """Everything on one tall page."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": width, "height": 900})
        pg.goto("file://" + os.path.abspath(html_path), wait_until="networkidle")
        pg.emulate_media(media="screen")
        h = pg.evaluate("document.documentElement.scrollHeight")
        pg.pdf(path=pdf_path, width=f"{width}px", height=f"{h + 2}px", print_background=True,
               margin={"top": "0", "bottom": "0", "left": "0", "right": "0"})
        b.close()
    print(f"  pdf: single page, {width}x{h}px", file=sys.stderr)


# Keep only one issue's own content (its nested sub-issues go to their own pages).
ISOLATE_JS = """([n, crumb, pages]) => {
  const s = document.getElementById('i' + n).cloneNode(true);
  s.querySelectorAll('section').forEach(x => x.remove());
  s.className = 'd0';
  // TOC (root page only): links can't jump inside a merged PDF -> show page numbers instead
  s.querySelectorAll('.toc a').forEach(a => {
    const num = a.getAttribute('href').slice(2);
    const sp = document.createElement('span');
    sp.innerHTML = a.innerHTML + ' <span class="pg">p.' + pages[num] + '</span>';
    a.replaceWith(sp);
  });
  const w = document.querySelector('.w'), foot = document.querySelector('.foot');
  w.replaceChildren();
  if (crumb) { const c = document.createElement('div'); c.className = 'crumb'; c.textContent = crumb; w.append(c); }
  w.append(s, foot);
  const st = document.createElement('style');
  st.textContent = '.crumb{color:#59636e;font-size:12px;margin-bottom:10px}.pg{color:#8c959f;font-size:12px;margin-left:6px}'
    + 'section.d0>h1,section.d0>h2,section.d0>h3,section.d0>h4,section.d0>h5,section.d0>h6{font-size:24px;font-weight:400;margin:0 0 8px}';
  document.head.append(st);
}"""


def to_pdf_paged(root, html_path, pdf_path, width=950):
    """One PDF page per issue; each page as tall as that issue's content. PDF bookmarks mirror the tree."""
    import io
    from playwright.sync_api import sync_playwright
    from pypdf import PdfReader, PdfWriter
    items = []  # (issue, depth, ancestors)

    def rec(it, anc):
        items.append((it, len(anc), anc))
        for s in it["_subs"]:
            rec(s, anc + [it])
    rec(root, [])
    pages = {str(it["number"]): i + 1 for i, (it, _, _) in enumerate(items)}
    writer = PdfWriter()
    url = "file://" + os.path.abspath(html_path)
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": width, "height": 100})
        pg.emulate_media(media="screen")
        for it, d, anc in items:
            pg.goto(url, wait_until="networkidle")
            crumb = " › ".join(f"#{a['number']} {a['title']}" for a in anc)
            pg.evaluate(ISOLATE_JS, [it["number"], crumb, pages])
            h = pg.evaluate("document.documentElement.scrollHeight")
            pdf = pg.pdf(width=f"{width}px", height=f"{h + 2}px", print_background=True,
                         margin={"top": "0", "bottom": "0", "left": "0", "right": "0"})
            writer.append(PdfReader(io.BytesIO(pdf)))
        b.close()
    marks = {}
    for i, (it, d, anc) in enumerate(items):
        parent = marks.get(anc[-1]["number"]) if anc else None
        marks[it["number"]] = writer.add_outline_item(f"#{it['number']} {it['title']}", i, parent=parent)
    writer.page_mode = "/UseOutlines"
    with open(pdf_path, "wb") as f:
        writer.write(f)
    print(f"  pdf: {len(items)} pages (one per issue)", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("issue", type=int)
    ap.add_argument("--out", default="exports")
    ap.add_argument("--no-pdf", action="store_true")
    ap.add_argument("--single-page", action="store_true", help="everything on one tall page instead of one page per issue")
    a = ap.parse_args()
    if not REPO:
        sys.exit("REPO (owner/name) not set")
    d = os.path.join(a.out, f"issue-{a.issue}")
    os.makedirs(d, exist_ok=True)
    print(f"Fetching #{a.issue} from {REPO} ...", file=sys.stderr)
    root = fetch(a.issue, set())
    imgmap = collect_images(root, os.path.join(d, "images"))  # immediately: signed URLs expire
    base = os.path.join(d, f"issue-{a.issue}")
    with open(base + ".md", "w", encoding="utf-8") as f:
        f.write(to_md(root, imgmap))
    with open(base + ".html", "w", encoding="utf-8") as f:
        f.write(to_html(root, imgmap))
    if not a.no_pdf:
        if a.single_page:
            to_pdf_single(base + ".html", base + ".pdf")
        else:
            to_pdf_paged(root, base + ".html", base + ".pdf")
    print(d)


if __name__ == "__main__":
    main()
