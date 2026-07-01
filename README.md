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

## 使用

将某个 skill 目录软链或复制到 `~/.claude/skills/` 即可在 Claude Code 中通过 Skill 工具或 `/<skill-name>` 触发。

```bash
ln -s "$(pwd)/skills/<skill-name>" ~/.claude/skills/<skill-name>
```

## 创建新 skill

参考官方 `skill-creator` skill，或直接在本仓库新建目录并添加 `SKILL.md`（需含 `name` 与 `description` frontmatter）。
