#!/usr/bin/env python3
"""
一次性初始化 llm-learning：建仓库、issue、sub-issue、Project 看板，并配置 Actions。
只用 Python 标准库 + 你本机已登录的 gh。可重复运行：已存在的仓库、issue、Project、字段会跳过。

用法（在仓库根目录）：
  gh auth refresh -s project            # 一次即可，给 gh 加上 Project 权限
  python3 bootstrap/setup.py            # 默认 lkebnl/llm-learning，public
  python3 bootstrap/setup.py --private  # 私有仓库
  python3 bootstrap/setup.py --dry-run  # 只打印要执行的命令
"""
import argparse, json, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLAN = json.loads((ROOT / "bootstrap" / "plan.json").read_text(encoding="utf-8"))
STATUS_OPTIONS = [("Backlog", "GRAY"), ("Todo", "BLUE"), ("In Progress", "YELLOW"),
                  ("Done", "GREEN"), ("Blocked", "RED")]

DRY = False
_fake = 1000


def gh(*args, json_out=False, check=True, input=None):
    global _fake
    cmd = ["gh", *map(str, args)]
    if DRY:
        print("  $", " ".join(cmd))
        _fake += 1
        if not json_out:
            return ""
        a = " ".join(cmd)
        if "issue list" in a:
            return []
        if "project list" in a:
            return {"projects": []}
        if "project create" in a:
            return {"number": 9, "id": "PVT_fake"}
        if "field-list" in a:
            return {"fields": [{"name": "Status", "id": "F_status", "options": []}]}
        if "field-create" in a:
            return {"id": f"F_{_fake}"}
        if "item-add" in a:
            return {"id": f"ITEM_{_fake}"}
        if "repos/" in a and "/issues/" in a:
            return {"id": _fake, "node_id": f"I_{_fake}"}
        if "graphql" in a:
            return {"data": {"updateProjectV2Field": {"projectV2Field": {"options": [
                {"id": f"O_{n}", "name": n} for n, _ in STATUS_OPTIONS]}}}}
        return {}
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", input=input)
    if check and r.returncode != 0:
        sys.exit(f"失败：{' '.join(cmd)}\n{r.stderr.strip()}")
    if json_out:
        return json.loads(r.stdout or "null")
    return r.stdout.strip()


def step(msg):
    print(f"\n== {msg}")


def issue_body(w):
    lines = [f"**主题**：{w['topic']}", ""]
    if w["resources"]:
        lines += ["**资料**", ""] + [f"- {r}" for r in w["resources"]] + [""]
    lines += ["**动手**", ""] + [f"- [ ] {t}" for t in w["tasks"]] + [""]
    lines += ["**完成标准（检查点）**", ""] + [f"- [ ] {d}" for d in w["done"]] + [""]
    lines += [f"计划时间：{w['start']} → {w['target']}", "",
              "进展写在评论里：做了什么、数值（loss、显存、耗时）、卡在哪。检查点都勾上再关闭。"]
    return "\n".join(lines)


def ensure_git_repo():
    if (ROOT / ".git").exists():
        return
    step("初始化本地 git")
    for c in (["git", "init", "-b", "main"], ["git", "add", "-A"],
              ["git", "commit", "-m", "Initial commit: llm-learning skeleton"]):
        print("  $", " ".join(c))
        if not DRY:
            subprocess.run(c, cwd=ROOT, check=True)


def ensure_repo(repo, private):
    step(f"仓库 {repo}")
    exists = not DRY and subprocess.run(["gh", "repo", "view", repo], capture_output=True).returncode == 0
    if exists:
        print("  已存在，跳过创建")
        return
    ensure_git_repo()
    gh("repo", "create", repo, "--private" if private else "--public",
       "--description", "大模型学习：按周计划、issue 记录、自动日志与周报",
       "--source", ROOT, "--remote", "origin", "--push")


def configure_actions(repo):
    step("Actions：workflow 权限设为 read and write")
    gh("api", "-X", "PUT", f"repos/{repo}/actions/permissions/workflow",
       "-f", "default_workflow_permissions=write")


def create_issues(repo):
    step("Issue")
    existing = {} if DRY else {i["title"]: i["number"] for i in
                               gh("issue", "list", "-R", repo, "--state", "all", "--limit", "500",
                                  "--json", "title,number", json_out=True)}

    def make(title, body):
        if title in existing:
            print(f"  已存在 #{existing[title]} {title}")
            return existing[title]
        url = gh("issue", "create", "-R", repo, "--title", title, "--body", body)
        num = int(url.rstrip("/").split("/")[-1]) if url else len(existing) + 1
        existing[title] = num
        print(f"  #{num} {title}")
        return num

    nums = {}
    for e in PLAN["epics"]:
        nums[e["key"]] = make(e["title"], e["body"])
    nums["inbox"] = make(PLAN["inbox"]["title"], PLAN["inbox"]["body"])
    for w in PLAN["weeks"]:
        nums[w["title"]] = make(w["title"], issue_body(w))
    return nums


def link_sub_issues(repo, nums):
    step("Sub-issue：每周任务挂到所属阶段下")
    for w in PLAN["weeks"]:
        parent, child = nums[w["epic"]], nums[w["title"]]
        cid = gh("api", f"repos/{repo}/issues/{child}", json_out=True)["id"]
        r = None if DRY else subprocess.run(
            ["gh", "api", f"repos/{repo}/issues/{parent}/sub_issues", "-F", f"sub_issue_id={cid}"],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        if DRY:
            print(f"  $ gh api repos/{repo}/issues/{parent}/sub_issues -F sub_issue_id={cid}")
        elif r.returncode != 0 and "already" not in (r.stderr + r.stdout).lower():
            print(f"  警告：#{child} → #{parent} 失败：{r.stderr.strip() or r.stdout.strip()}")
        else:
            print(f"  #{child} → #{parent}")


def ensure_project(owner, repo):
    step("Project 看板")
    title = PLAN["project_title"]
    projects = gh("project", "list", "--owner", owner, "--format", "json", "--limit", "100", json_out=True)
    proj = next((p for p in projects.get("projects", []) if p.get("title") == title), None)
    if proj:
        print(f"  已存在 #{proj['number']}")
    else:
        proj = gh("project", "create", "--owner", owner, "--title", title, "--format", "json", json_out=True)
        print(f"  新建 #{proj['number']}")
    num, pid = proj["number"], proj["id"]
    gh("project", "link", num, "--owner", owner, "--repo", repo, check=False)
    return num, pid


def ensure_fields(owner, num):
    step("Project 字段：Status 选项、Start / Target 日期")
    fields = gh("project", "field-list", num, "--owner", owner, "--format", "json", "--limit", "50",
                json_out=True)["fields"]
    by = {f["name"]: f for f in fields}
    status = by["Status"]
    have = [o["name"] for o in status.get("options", [])]
    want = [n for n, _ in STATUS_OPTIONS]
    if have != want:
        opts = ", ".join(f'{{name: "{n}", color: {c}, description: ""}}' for n, c in STATUS_OPTIONS)
        q = ('mutation { updateProjectV2Field(input: {fieldId: "%s", singleSelectOptions: [%s]}) '
             '{ projectV2Field { ... on ProjectV2SingleSelectField { options { id name } } } } }'
             % (status["id"], opts))
        r = gh("api", "graphql", "-f", f"query={q}", json_out=True, check=False)
        try:
            status["options"] = r["data"]["updateProjectV2Field"]["projectV2Field"]["options"]
            print("  Status：" + " / ".join(want))
        except (TypeError, KeyError):
            print("  警告：无法修改 Status 选项，保留默认（Todo / In Progress / Done）。"
                  "可在看板里手动加 Backlog、Blocked 后重跑本脚本。")
    for name in ("Start", "Target"):
        if name not in by:
            by[name] = gh("project", "field-create", num, "--owner", owner, "--name", name,
                          "--data-type", "DATE", "--format", "json", json_out=True)
            print(f"  新建日期字段 {name}")
    opt = {o["name"]: o["id"] for o in status.get("options", [])}
    return status["id"], opt, by["Start"]["id"], by["Target"]["id"]


def add_items(owner, repo, num, pid, nums, status_id, opt, start_id, target_id):
    step("把 issue 加入看板，设置 Status 和日期")
    rows = [(e["title"], nums[e["key"]], e["status"], e["start"], e["target"]) for e in PLAN["epics"]]
    rows.append((PLAN["inbox"]["title"], nums["inbox"], PLAN["inbox"]["status"], None, None))
    rows += [(w["title"], nums[w["title"]], w["status"], w["start"], w["target"]) for w in PLAN["weeks"]]
    for title, n, st, start, target in rows:
        item = gh("project", "item-add", num, "--owner", owner, "--format", "json",
                  "--url", f"https://github.com/{repo}/issues/{n}", json_out=True)["id"]
        o = opt.get(st) or opt.get("Todo")
        if o:
            gh("project", "item-edit", "--id", item, "--project-id", pid,
               "--field-id", status_id, "--single-select-option-id", o)
        for fid, d in ((start_id, start), (target_id, target)):
            if d:
                gh("project", "item-edit", "--id", item, "--project-id", pid, "--field-id", fid, "--date", d)
        print(f"  #{n} {st}  {start or ''} {target or ''}")


def configure_plan_access(repo, num, ask_token):
    step("计划读取看板 Status：Variable PROJECT_NUMBER、Secret PROJECT_TOKEN")
    gh("variable", "set", "PROJECT_NUMBER", "--body", num, "-R", repo)
    if not ask_token:
        print("  跳过 PROJECT_TOKEN（--no-token）。不配时计划按最近更新列出 open issue。")
        return
    ans = "y" if DRY else input(
        "  用当前 gh 登录的 token 作为 PROJECT_TOKEN？更稳妥的做法是另建只含 read:project 的 classic PAT。[y/N] ")
    if ans.strip().lower() == "y":
        tok = "" if DRY else gh("auth", "token")
        gh("secret", "set", "PROJECT_TOKEN", "-R", repo, input=tok) if not DRY else \
            print(f"  $ gh secret set PROJECT_TOKEN -R {repo}  (stdin: gh auth token)")
    else:
        print(f"  稍后手动：gh secret set PROJECT_TOKEN -R {repo}")


def main():
    global DRY
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--owner", default="lkebnl")
    p.add_argument("--name", default="llm-learning")
    p.add_argument("--private", action="store_true")
    p.add_argument("--no-token", action="store_true", help="不设置 PROJECT_TOKEN secret")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()
    DRY = a.dry_run
    repo = f"{a.owner}/{a.name}"
    if not DRY and subprocess.run(["gh", "auth", "status"], capture_output=True).returncode != 0:
        sys.exit("gh 未登录：先运行 gh auth login，再运行 gh auth refresh -s project")

    ensure_repo(repo, a.private)
    configure_actions(repo)
    nums = create_issues(repo)
    link_sub_issues(repo, nums)
    num, pid = ensure_project(a.owner, repo)
    status_id, opt, start_id, target_id = ensure_fields(a.owner, num)
    add_items(a.owner, repo, num, pid, nums, status_id, opt, start_id, target_id)
    configure_plan_access(repo, num, not a.no_token)

    step("完成")
    print(f"  仓库：https://github.com/{repo}")
    print(f"  看板：https://github.com/users/{a.owner}/projects/{num}")
    print("  剩下两步在网页上做（API 不支持建视图）：")
    print("   1. 看板里加 Board 视图（按 Status 分列）和 Roadmap 视图（Date fields 选 Start / Target）")
    print("   2. Actions 页面手动 Run workflow 一次 Morning plan（勾选 force）验证")


if __name__ == "__main__":
    main()
