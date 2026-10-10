"""Sanity-check every skill in skills/: frontmatter, references, and scripts.

Each skills/<name>/SKILL.md must start with YAML frontmatter whose `name`
matches the directory and whose `description` is non-empty (that's what
Claude Code loads to decide when to use the skill). Every relative link in
SKILL.md must resolve, and every Python script must run `--help` cleanly.
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
errors = []

for skill_md in sorted(ROOT.glob("skills/*/SKILL.md")):
    skill = skill_md.parent
    text = skill_md.read_text()
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not m:
        errors.append(f"{skill_md}: missing frontmatter")
        continue
    fields = dict(re.findall(r"^(\w+):\s*(.+)$", m.group(1), re.M))
    if fields.get("name") != skill.name:
        errors.append(f"{skill_md}: name {fields.get('name')!r} != directory {skill.name!r}")
    if not fields.get("description", "").strip():
        errors.append(f"{skill_md}: empty description")

    for link in re.findall(r"\]\(([^)#\s]+)\)", text):
        if "://" not in link and not (skill / link).exists():
            errors.append(f"{skill_md}: broken link {link}")

    for script in sorted(skill.glob("scripts/*.py")):
        r = subprocess.run([sys.executable, str(script), "--help"], capture_output=True, text=True)
        if r.returncode != 0:
            errors.append(f"{script}: --help exited {r.returncode}: {r.stderr.strip()[:200]}")

    print(f"checked {skill.name}")

if errors:
    print("\n".join(errors), file=sys.stderr)
    sys.exit(1)
print("all skills OK")
