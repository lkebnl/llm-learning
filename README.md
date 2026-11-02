# llm-learning

大模型学习：12 周计划，用 GitHub Issues + Projects 记录，结构和自动化沿用 [work-log](https://github.com/lkebnl/work-log)。

## 每周精力分布

![每周精力分布](charts/effort-latest.png)

每周日晚自动更新 · 周报见 [reports/](reports/) · 每日记录见 [logs/](logs/) · 每日计划见 [plans/](plans/)

## 结构

| 层级 | 内容 | 状态 |
|---|---|---|
| 阶段 issue：`[基础]` `[Transformer]` `[预训练]` `[后训练]` `[推理]` | 相当于 Epic | 阶段内各周都关闭后再关 |
| 每周 sub-issue：`W00`–`W12` | 主题、资料、动手任务、完成标准（检查点） | 检查点都勾上就关闭 |
| `[Inbox] 问题与想法` | 零散的问题和想法 | 长期开着 |
| comment | 每次学习的进展 | — |

里程碑：**W08 核心概念**（约 2026-12-27），**W12 全链路**（约 2027-01-24）。

## 怎么记

- 每次学完在当周 issue 下写一条评论：做了什么、关键数值（loss、显存、耗时、参数量）、卡在哪。只写“看完了”没有信息量。
- 想问 Claude 的问题先写进评论或 Inbox，带上报错和配置，方便之后回看。
- 看板状态：Backlog → Todo → In Progress → Done / Blocked。每周日把下周的 issue 拖到 Todo，当前在做的拖到 In Progress。
- 落后了就整体顺延日期，不要跳过 W03–W05。

## 自动产出（纽约时间，全部由代码生成，不调用 AI）

学习主要在晚上和周末，所以和 work-log 不同，每天都跑：

| 时间 | workflow | 输出 |
|---|---|---|
| 每天 06:17 起 | `morning-plan.yml` | `plans/YYYY/日期.md`：In Progress / Todo / Blocked，附最近一条评论 |
| 每天 22:17 起 | `daily-log.yml` | `logs/YYYY/日期.md`：当天所有评论和状态变化 |
| 周日 22:20 起 | `weekly-summary.yml` | `reports/YYYY/YYYY-Www.md` + `charts/` 精力分布图 |

和 work-log 相比的改动：`worklog.py` 增加两个环境变量 `WORKLOG_DAYS=all`（周末也运行）和 `WEEK_DAY=6`（周报在周日），默认值与原版一致；去掉了板号索引。其余说明见 work-log 的 [help.md](https://github.com/lkebnl/work-log/blob/main/help.md) 和 [worklog.md](https://github.com/lkebnl/work-log/blob/main/.github/scripts/worklog.md)。

## 初始化（一次性）

```bash
gh auth refresh -s project          # 给 gh 加 Project 权限
python3 bootstrap/setup.py          # 默认 lkebnl/llm-learning，public；--private 改私有；--dry-run 只打印命令
```

脚本会：建仓库并推送 → workflow 权限设为 read and write → 建 5 个阶段 issue、Inbox 和 13 个每周 issue → 挂成 sub-issue → 建 Project「大模型学习」，Status 设为 Backlog / Todo / In Progress / Done / Blocked，加 Start / Target 日期字段并填好 → 设置 `PROJECT_NUMBER` 和（可选）`PROJECT_TOKEN`。可重复运行，已存在的会跳过。

之后在网页上：

1. 看板加 Board 视图（按 Status 分列）和 Roadmap 视图（Date fields 选 Start / Target）。
2. Actions → *Morning plan* → Run workflow（勾选 force）验证一次。

计划内容的源文件是 [bootstrap/plan.json](bootstrap/plan.json)。

## 4060 备注

8GB 显存：从零训练几十 M 到约 150M 参数（BF16 + 梯度累积）；QLoRA 3–4B 舒服、7–8B 勉强；llama.cpp Q4 的 7–8B 可全部放进显存。
