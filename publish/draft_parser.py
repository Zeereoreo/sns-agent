"""drafts/*.md 초안을 게시용 구조로 파싱한다.

초안 형식:
  <!-- 메타(제목안/태그/이미지) --> ... # 제목 ... 본문 ... [이미지..] ... #태그
반환:
  {title: str, tags: list[str], blocks: [{kind, text|alt}, ...]}
  blocks.kind ∈ {"heading", "text", "image"}
"""
from __future__ import annotations

import re
from pathlib import Path


def _extract_comment(text: str) -> str:
    m = re.search(r"<!--(.*?)-->", text, re.S)
    return m.group(1) if m else ""


def _clean_inline(s: str) -> str:
    """마크다운 강조/코드 기호 제거 (스마트에디터는 평문 입력)."""
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)   # **굵게** -> 굵게
    s = re.sub(r"`(.+?)`", r"\1", s)
    return s.strip()


def parse_draft(path: str | Path) -> dict:
    text = Path(path).read_text(encoding="utf-8")
    comment = _extract_comment(text)
    body = re.sub(r"<!--.*?-->", "", text, flags=re.S).strip()

    # --- 제목: 메타의 '제목안: 1)' 우선, 없으면 첫 H1 ---
    title = None
    m = re.search(r"제목안[:\s]*1\)\s*(.+)", comment)
    if m:
        title = m.group(1).strip()
    if not title:
        m = re.search(r"^#\s+(.+)$", body, re.M)
        title = m.group(1).strip() if m else "제목 없음"

    # --- 태그: 메타의 '태그:' 우선, 없으면 본문 마지막 해시태그 줄 ---
    tags: list[str] = []
    m = re.search(r"^태그[:\s]*(.+)$", comment, re.M)
    tag_src = m.group(1) if m else ""
    if not tag_src:
        for line in reversed(body.splitlines()):
            if line.strip().startswith("#") and " #" in line:
                tag_src = line
                break
    tags = [t.lstrip("#").strip() for t in tag_src.split() if t.startswith("#")]

    # --- 본문 블록화 ---
    blocks: list[dict] = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("# ") and not line.startswith("## "):  # H1(제목) 은 본문에서 제외
            continue
        # 소제목(##/### …) 은 해시태그 필터보다 먼저 처리한다.
        # (^#\S 필터가 '## 소제목'을 해시태그로 오인해 통째로 버리던 버그)
        if line.startswith("## ") or line.startswith("### "):
            blocks.append({"kind": "heading", "text": _clean_inline(line.lstrip("# ").strip())})
            continue
        if re.match(r"^#\S", line) or (line.startswith("#") and " #" in line):
            continue                          # 해시태그 줄 제외
        img = re.match(r"^\[이미지.*?(?:ALT\s*[\"“](.+?)[\"”])?\]$", line)
        if line.startswith("[이미지"):
            alt = img.group(1) if img and img.group(1) else ""
            blocks.append({"kind": "image", "alt": alt})
            continue
        blocks.append({"kind": "text", "text": _clean_inline(line)})

    return {"title": title, "tags": tags, "blocks": blocks}


if __name__ == "__main__":
    import sys, json
    d = parse_draft(sys.argv[1])
    print("제목:", d["title"])
    print("태그:", d["tags"])
    print("블록수:", len(d["blocks"]), "(이미지",
          sum(1 for b in d["blocks"] if b["kind"] == "image"), "개)")
    print(json.dumps(d["blocks"][:6], ensure_ascii=False, indent=2))
