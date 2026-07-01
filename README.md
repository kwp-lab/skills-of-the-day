# skills-of-the-day

日常使用的 Claude Code skills 集合，方便日常复用与共享。

## 结构

所有 skill 放在 `skills/` 下，每个 skill 一个目录，包含 `SKILL.md` 和相关脚本/资源：

```
skills-of-the-day/
├── skills/
│   └── <skill-name>/
│       ├── SKILL.md
│       └── ...
└── README.md
```

## How to

使用 [`skills` CLI](https://www.npmjs.com/package/skills) 一句话即可安装本仓库内的 skills，无需手动 clone 或建软链。

**安装全部 skills（推荐）：**

```bash
npx skills add kwp-lab/skills-of-the-day --all
```

**安装指定 skill：**

```bash
npx skills add kwp-lab/skills-of-the-day --skill featured-marketplace-content
```

**安装到指定 agent（例如仅 Claude Code）：**

```bash
npx skills add kwp-lab/skills-of-the-day -a claude-code
```

**全局安装（写入 `~/.claude/skills/` 而不是当前项目）：**

```bash
npx skills add kwp-lab/skills-of-the-day --all -g
```

**CI/非交互模式（跳过所有确认）：**

```bash
npx skills add kwp-lab/skills-of-the-day --skill featured-marketplace-content -g -a claude-code -y
```

**不安装、临时使用：**

```bash
npx skills use kwp-lab/skills-of-the-day@featured-marketplace-content | claude
```

常用参数：

| 参数 | 说明 |
| --- | --- |
| `--all` | 一次性安装所有 skill 到所有 agent |
| `-s, --skill <name>` | 指定要安装的 skill，可重复传入 |
| `-a, --agent <name>` | 指定目标 agent（如 `claude-code`、`opencode`） |
| `-g, --global` | 装到用户目录而不是当前项目 |
| `-y, --yes` | 跳过交互确认 |

## 创建新 skill

参考官方 `skill-creator` skill，或直接在 `skills/` 下新建目录并添加 `SKILL.md`（需含 `name` 与 `description` frontmatter）。
