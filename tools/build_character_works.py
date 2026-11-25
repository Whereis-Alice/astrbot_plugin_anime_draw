"""Build the character work-title index from a local Bangumi Archive ZIP."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from util.work_sources import (
    WORK_TYPES,
    has_bangumi_id,
    take_titles,
    work_title,
)

DATE = re.compile(r"^[1-9][0-9]{3}(?:-[0-9]{2}){0,2}$")


def read_archive(archive: ZipFile, name: str):
    with archive.open(name) as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def build_index(archive: ZipFile, character_ids: set[int]) -> dict[int, tuple[str, ...]]:
    by_character: dict[int, list[tuple[int, int]]] = defaultdict(list)
    related_subjects = set()
    for row in read_archive(archive, "subject-characters.jsonlines"):
        character_id = row["character_id"]
        if character_id in character_ids:
            subject_id = row["subject_id"]
            by_character[character_id].append((row["type"], subject_id))
            related_subjects.add(subject_id)

    titles = {}
    for row in read_archive(archive, "subject.jsonlines"):
        subject_id = row["id"]
        if subject_id not in related_subjects or row["type"] not in WORK_TYPES:
            continue
        title = work_title(row)
        if title:
            date = str(row.get("date") or "")
            titles[subject_id] = (date if DATE.fullmatch(date) else "9999-12-31", title)

    index = {}
    for character_id, links in sorted(by_character.items()):
        ranked = sorted(
            (role, titles[subject_id][0], subject_id, titles[subject_id][1])
            for role, subject_id in links
            if subject_id in titles
        )
        if ranked:
            index[character_id] = take_titles(title for _, _, _, title in ranked)
    return index


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path, help="Bangumi Archive ZIP release")
    parser.add_argument("--archive-name", help="Original release filename if the file was renamed")
    parser.add_argument("--output", type=Path, default=ROOT / "util" / "character_works.json")
    args = parser.parse_args()

    catalog_path = ROOT / "util" / "characters.json"
    characters = json.loads(catalog_path.read_text(encoding="utf-8"))
    ids = {char["id"] for char in characters if has_bangumi_id(char)}
    with ZipFile(args.archive) as archive:
        index = build_index(archive, ids)

    release_name = args.archive_name or args.archive.name
    source = json.dumps(
        {"project": "https://github.com/bangumi/Archive", "archive": release_name},
        ensure_ascii=False,
    )
    lines = [
        f"    {json.dumps(str(character_id))}: {json.dumps(works, ensure_ascii=False)}"
        for character_id, works in index.items()
    ]
    data = ",\n".join(lines)
    args.output.write_text(
        f'{{\n  "source": {source},\n  "works": {{\n{data}\n  }}\n}}\n', encoding="utf-8"
    )
    print(f"{len(index):,}/{len(characters):,} characters matched ({len(ids):,} verified IDs).")


if __name__ == "__main__":
    main()
