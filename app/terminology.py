"""术语审校引擎。

按语言区分的术语表（规范词 / 允许变体 / 禁止替代词）在全部 cue 上做跨
cue 一致性检测，覆盖四类差异：

- 大小写差异（Wi-Fi vs WI-FI）
- 全半角差异（HDMI vs ｈｄｍｉ）
- 标点 / 空白差异（e-mail vs email）
- 近似拼写差异（website vs websyte，拉丁词与 CJK 词分别处理）

检测分三层，先占先得、逐层放宽：
1. 严格匹配：在 NFKC + casefold 归一化文本上匹配，保留原始表面形式以便
   区分大小写与全半角问题；
2. 紧凑匹配：去除标点与空白后匹配，专门捕捉标点 / 空白差异；
3. 模糊匹配：对规范词做编辑距离阈值内的近似拼写扫描。

禁止替代词在任何一层命中都直接标出；允许变体本身合规，但当其他 cue 使用
规范词时，会以「变体混用」提示统一。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from .subtitles import Cue, detect_language

# --- 类型常量 ---------------------------------------------------------------

CANONICAL = "canonical"
VARIANT = "variant"
FORBIDDEN = "forbidden"
CASE_MISMATCH = "case_mismatch"
WIDTH_MISMATCH = "width_mismatch"
PUNCT_MISMATCH = "punctuation_mismatch"
FUZZY = "fuzzy"
VARIANT_MIXED = "variant_mixed"

ISSUE_TYPES = {
    FORBIDDEN, CASE_MISMATCH, WIDTH_MISMATCH,
    PUNCT_MISMATCH, FUZZY, VARIANT_MIXED,
}
TYPE_LABELS = {
    FORBIDDEN: "禁止替代词",
    CASE_MISMATCH: "大小写差异",
    WIDTH_MISMATCH: "全半角差异",
    PUNCT_MISMATCH: "标点差异",
    FUZZY: "近似拼写",
    VARIANT_MIXED: "允许变体混用",
}
TYPE_SEVERITY = {
    FORBIDDEN: "error",
    CASE_MISMATCH: "warn",
    WIDTH_MISMATCH: "warn",
    PUNCT_MISMATCH: "warn",
    FUZZY: "warn",
    VARIANT_MIXED: "info",
}

_CJK_RUN_RE = re.compile(r"[㐀-䶿一-鿿豈-﫿]+")
_LATIN_RUN_RE = re.compile(r"[a-z0-9]+")
_SPLIT_RE = re.compile(r"[,，;；\n]")

# 语言检测是启发式的，短句常落到 other / unknown；把这两个检测失败桶
# 回退匹配到各已知文字系统，避免「Check our websyte now.」漏掉英文术语。
# 不在 zh/ja/ko 或 en/es/fr/de 等已确定语言之间互串。
_LATIN_LANGS = {"en", "es", "fr", "de"}
_CJK_LANGS = {"zh", "ja", "ko"}


def _lang_matches(term_lang: str, cue_lang: str) -> bool:
    if term_lang == "any":
        return True
    if term_lang == cue_lang:
        return True
    if cue_lang == "other" and term_lang in _LATIN_LANGS:
        return True
    if cue_lang == "unknown" \
            and (term_lang in _LATIN_LANGS or term_lang in _CJK_LANGS):
        return True
    return False


@dataclass
class GlossaryTerm:
    canonical: str
    language: str = "any"
    allowed: list[str] = field(default_factory=list)
    forbidden: list[str] = field(default_factory=list)
    note: str = ""
    id: int | None = None


# --- 归一化 -----------------------------------------------------------------

def _norm(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def _norm_map(text: str) -> tuple[str, list[int]]:
    """NFKC + casefold，同时返回每个输出字符对应的原文索引。"""
    out: list[str] = []
    idx: list[int] = []
    for i, ch in enumerate(text):
        for c in _norm(ch):
            out.append(c)
            idx.append(i)
    return "".join(out), idx


def _compact(text: str) -> str:
    """去掉标点与空白后的紧凑形态（e-mail → email）。"""
    return "".join(ch for ch in text if ch.isalnum())


def _is_latin_word(text: str) -> bool:
    return bool(text) and all(
        ("a" <= ch.lower() <= "z") or ch.isdigit()
        for ch in text
    ) and any("a" <= ch.lower() <= "z" for ch in text)


def split_variants(raw: str | list | None) -> list[str]:
    """把逗号 / 顿号 / 换行分隔的变体字符串拆成去重列表。"""
    if raw is None:
        return []
    if isinstance(raw, list):
        items = [str(x).strip() for x in raw]
    else:
        items = _SPLIT_RE.split(str(raw))
    result: list[str] = []
    seen: set[str] = set()
    for it in items:
        it = it.strip()
        if it and it not in seen:
            seen.add(it)
            result.append(it)
    return result


def make_term(language: str, canonical: str,
              allowed: str | list | None = None,
              forbidden: str | list | None = None,
              note: str = "", term_id: int | None = None) -> GlossaryTerm:
    canonical = (canonical or "").strip()
    if not canonical:
        raise ValueError("规范词不能为空")
    term = GlossaryTerm(
        canonical=canonical,
        language=(language or "any").strip() or "any",
        allowed=split_variants(allowed),
        forbidden=split_variants(forbidden),
        note=(note or "").strip(),
        id=term_id,
    )
    if _norm(term.canonical) in {_norm(v) for v in term.allowed}:
        raise ValueError(f"允许变体不能与规范词相同：{canonical}")
    if _norm(term.canonical) in {_norm(v) for v in term.forbidden}:
        raise ValueError(f"禁止替代词不能与规范词相同：{canonical}")
    return term


# --- 编辑距离 ----------------------------------------------------------------

def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(
                prev[j] + 1, cur[j - 1] + 1,
                prev[j - 1] + (ca != cb),
            ))
        prev = cur
    return prev[-1]


def _fuzzy_close(a: str, b: str) -> bool:
    """近似拼写阈值：短词 1 处、长词 2 处以内编辑，且一方不能是另一方的
    子串（避免 mail/email 这类误报，websyte/website 仍可命中）。"""
    if a == b:
        return False
    if abs(len(a) - len(b)) > 2:
        return False
    if a in b or b in a:
        return False
    d = _levenshtein(a, b)
    if len(a) <= 4:
        return d == 1
    return d <= 2 and d / max(len(a), len(b)) <= 0.25


# --- 单 cue 扫描 -------------------------------------------------------------

@dataclass
class _Hit:
    begin: int           # cue 文本中的字符偏移
    end: int
    surface: str
    kind: str            # canonical / variant / forbidden / case/.../fuzzy
    matched_form: str    # 命中的词表原形（规范词/某个变体/禁止词）


def _latin_alnum(ch: str) -> bool:
    return ch.isascii() and ch.isalnum()


def _strict_scan(norm: str, idx: list[int], form: str,
                 latin: bool) -> list[tuple[int, int]]:
    """在归一化文本中做带词边界的严格查找，返回归一化坐标区间。"""
    key = _norm(form)
    spans: list[tuple[int, int]] = []
    start = 0
    while True:
        pos = norm.find(key, start)
        if pos < 0:
            break
        end = pos + len(key)
        start = end
        if latin:
            if pos > 0 and _latin_alnum(norm[pos - 1]):
                continue
            if end < len(norm) and _latin_alnum(norm[end]):
                continue
        spans.append((pos, end))
    return spans


def _alnum_gap_pattern(nf: str) -> tuple[str, list[bool]]:
    """归一化字符串 → (字母数字拼接, 相邻字母数字之间是否夹有非字母数字)。"""
    chars = [c for c in nf if c.isalnum()]
    gaps: list[bool] = []
    seen_sep = False
    found_first = False
    for ch in nf:
        if ch.isalnum():
            if found_first:
                gaps.append(seen_sep)
            found_first = True
            seen_sep = False
        elif found_first:
            seen_sep = True
    return "".join(chars), gaps[:max(0, len(chars) - 1)]


def _compact_scan_any(norm: str, idx: list[int],
                      raw_keys: set[str], latin: bool
                      ) -> list[tuple[int, int, str]]:
    """去标点/空白后的窗口匹配：在每个窗口上检查 compact 形态是否命中某个
    原始 key。返回 (归一化起, 归一化止, 命中的原始 key)。

    窗口内被折叠的分隔位置必须与 key 内部分隔位置一致：「USB C」（空格）
    可以命中「USB-C」（连字符），而「usbc」（无分隔）不会。
    """
    # compact 形态 → (原始 key, 内部分隔模式)；同 compact 取最长原始形式
    key_info: dict[str, tuple[str, list[bool]]] = {}
    for raw in raw_keys:
        compact, gaps = _alnum_gap_pattern(_norm(raw))
        if compact and (compact not in key_info
                        or len(raw) > len(key_info[compact][0])):
            key_info[compact] = (raw, gaps)

    chars: list[str] = []
    positions: list[int] = []
    gap_before: list[bool] = []
    for k, ch in enumerate(norm):
        if ch.isalnum():
            prev_pos = positions[-1] if positions else -1
            gap_before.append(any(not norm[j].isalnum()
                                  for j in range(prev_pos + 1, k)))
            chars.append(ch)
            positions.append(k)
    compact = "".join(chars)
    # 与右邻 alnum 字符之间是否隔了被折叠的标点/空白
    gap_after = [False] * len(chars)
    for p in range(len(chars) - 1):
        gap_after[p] = any(not norm[j].isalnum()
                           for j in range(positions[p] + 1, positions[p + 1]))
    max_len = max((len(k) for k in key_info), default=0)
    if not max_len:
        return []

    spans: list[tuple[int, int, str]] = []
    for pos in range(len(compact)):
        # 从长到短，优先匹配更长的 key
        for length in range(min(max_len, len(compact) - pos), 0, -1):
            window = compact[pos:pos + length]
            info = key_info.get(window)
            if info is None:
                continue
            raw_key, key_gaps = info
            inner_gaps = gap_before[pos + 1:pos + length]
            # 标点/空白差异允许表面缺少分隔（usbc ↔ USB-C）或以空白替换
            # 标点（USB C ↔ USB-C），因此表面分隔模式必须是 key 分隔的
            # 子集：key 无分隔处表面也不能有分隔
            if any(g and not kg for g, kg in zip(inner_gaps, key_gaps)):
                continue
            if latin:
                # 拉丁词边界：相邻拉丁字母数字才算阻断；与左邻之间若存在
                # 被折叠的标点/空白，或左邻是 CJK，仍是有效边界
                if pos > 0 and _latin_alnum(compact[pos - 1]) \
                        and not gap_before[pos]:
                    continue
                right = pos + length
                if right < len(compact) \
                        and _latin_alnum(compact[right]) \
                        and not gap_after[pos + length - 1]:
                    continue
            lo = positions[pos]
            hi = positions[pos + length - 1] + 1
            spans.append((lo, hi, raw_key))
            break
    return spans


def _raw_span(text: str, idx: list[int], lo: int, hi: int) -> tuple[int, int, str]:
    begin = idx[lo]
    end = idx[hi - 1] + 1
    return begin, end, text[begin:end]


def _classify_canonical(surface: str, canonical: str) -> str:
    if surface == canonical:
        return CANONICAL
    # 全半角优先：NFKC 归一化相同即宽度差异（即使同时伴随大小写不同）
    if unicodedata.normalize("NFKC", surface).casefold() \
            == unicodedata.normalize("NFKC", canonical).casefold():
        if unicodedata.normalize("NFKC", surface) != surface or \
                unicodedata.normalize("NFKC", canonical) != canonical:
            return WIDTH_MISMATCH
        return CASE_MISMATCH
    return CANONICAL  # 理论不可达：严格匹配已保证归一化相同


def _overlaps(begin: int, end: int, occupied: list[tuple[int, int]]) -> bool:
    return any(begin < e and end > b for b, e in occupied)


def _scan_cue(text: str, term: GlossaryTerm) -> list[_Hit]:
    norm, idx = _norm_map(text)
    latin = _is_latin_word(_compact(_norm(term.canonical)))
    occupied: list[tuple[int, int]] = []
    hits: list[_Hit] = []

    def take(spans, kind, form):
        for lo, hi in spans:
            rb, re_, surface = _raw_span(text, idx, lo, hi)
            if not surface.strip() or _overlaps(rb, re_, occupied):
                continue
            occupied.append((rb, re_))
            hits.append(_Hit(rb, re_, surface, kind, form))

    # 1. 严格层：禁止词优先，其次规范词，最后允许变体（同形冲突时禁止词胜出）
    for form in term.forbidden:
        take(_strict_scan(norm, idx, form, latin), FORBIDDEN, form)

    canon_spans = _strict_scan(norm, idx, term.canonical, latin)
    for lo, hi in canon_spans:
        rb, re_, surface = _raw_span(text, idx, lo, hi)
        if _overlaps(rb, re_, occupied):
            continue
        occupied.append((rb, re_))
        hits.append(_Hit(rb, re_, surface,
                         _classify_canonical(surface, term.canonical),
                         term.canonical))

    for form in term.allowed:
        for lo, hi in _strict_scan(norm, idx, form, latin):
            rb, re_, surface = _raw_span(text, idx, lo, hi)
            if _overlaps(rb, re_, occupied):
                continue
            # 严格层只接受与变体原形逐字符相同的命中；仅全半角 /
            # 大小写相同的表面（如变体 WiFi 命中全角 ＷＩＦＩ）交给
            # 紧凑层按规范词做宽度 / 大小写判定
            if surface != form:
                continue
            occupied.append((rb, re_))
            hits.append(_Hit(rb, re_, surface, VARIANT, form))

    # 2. 紧凑层（标点 / 空白差异）：单次窗口扫描并校验内部分隔位置，
    #    按 禁止词 > 规范词 > 允许变体 的优先级分类
    forbidden_raw = set(term.forbidden)
    allowed_raw = set(term.allowed)
    all_raw = forbidden_raw | {term.canonical} | allowed_raw
    canon_alnum = "".join(c for c in term.canonical if c.isalnum())
    for lo, hi, matched in _compact_scan_any(norm, idx, all_raw, latin):
        rb, re_, surface = _raw_span(text, idx, lo, hi)
        if _overlaps(rb, re_, occupied):
            continue
        if matched in forbidden_raw:
            kind = FORBIDDEN
        elif matched == term.canonical:
            alnum_only = "".join(c for c in surface if c.isalnum())
            # NFKC 后形态发生变化（全角字母/数字/标点）→ 全半角差异，
            # 即使规范词带连字符（如 ＷＩＦＩ vs Wi-Fi）
            nfkc_surface = unicodedata.normalize("NFKC", surface)
            canon_has_sep = any(not c.isalnum() for c in term.canonical)
            if nfkc_surface != surface:
                kind = WIDTH_MISMATCH
            elif alnum_only == surface and canon_has_sep:
                # 表面完全无分隔而规范词带连字符/空格（usbc ↔ USB-C）
                kind = PUNCT_MISMATCH
            elif alnum_only == surface and len(alnum_only) == len(canon_alnum):
                # 表面与规范词字母数字串等长且表面无分隔：只剩大小写差异
                kind = CASE_MISMATCH
            else:
                # 其余（表面缺/多分隔、空白替换标点）→ 标点 / 空白差异
                kind = PUNCT_MISMATCH
        else:
            kind = VARIANT
        occupied.append((rb, re_))
        hits.append(_Hit(rb, re_, surface, kind, matched))

    # 3. 模糊层（仅对规范词做近似拼写）
    canon_compact = _compact(_norm(term.canonical))
    if latin and len(canon_compact) >= 3:
        for m in _LATIN_RUN_RE.finditer(norm):
            run = m.group(0)
            if len(run) < 3 or not _fuzzy_close(run, canon_compact):
                continue
            lo, hi = m.span()
            rb, re_, surface = _raw_span(text, idx, lo, hi)
            if _overlaps(rb, re_, occupied):
                continue
            occupied.append((rb, re_))
            hits.append(_Hit(rb, re_, surface, FUZZY, term.canonical))
    elif not latin and len(canon_compact) >= 3:
        # CJK：在连续汉字片段上滑窗，仅允许 1 字之差，降低误报
        for run in _CJK_RUN_RE.finditer(norm):
            frag = run.group(0)
            L = len(canon_compact)
            for p in range(0, len(frag) - L + 1):
                window = frag[p:p + L]
                if window == canon_compact or _levenshtein(window, canon_compact) > 1:
                    continue
                lo = run.start() + p
                hi = lo + L
                rb, re_, surface = _raw_span(text, idx, lo, hi)
                if _overlaps(rb, re_, occupied):
                    continue
                occupied.append((rb, re_))
                hits.append(_Hit(rb, re_, surface, FUZZY, term.canonical))

    hits.sort(key=lambda h: h.begin)
    return hits


# --- 跨 cue 审校 -------------------------------------------------------------

def _context(text: str, begin: int, end: int,
             radius: int = 14) -> tuple[str, str, str]:
    return (text[max(0, begin - radius):begin],
            text[begin:end],
            text[end:end + radius])


def review_terms(cues: list[Cue],
                 terms: list[GlossaryTerm]) -> dict:
    """对全部 cue 执行术语审校，返回 {findings, stats}。"""
    # 每个术语在每个 cue 上的命中
    # term_hits[term] = list of (cue_seq, hit)
    term_hits: dict[int, list[tuple[int, _Hit]]] = {id(t): [] for t in terms}
    cue_langs = [detect_language(c.text) for c in cues]

    for ci, cue in enumerate(cues):
        for term in terms:
            if not _lang_matches(term.language, cue_langs[ci]):
                continue
            for hit in _scan_cue(cue.text, term):
                term_hits[id(term)].append((ci, hit))

    findings: list[dict] = []
    by_type: dict[str, int] = {}

    for term in terms:
        occ = term_hits[id(term)]
        if not occ:
            continue
        cue_set = {ci for ci, _ in occ}
        cross_cue = len(cue_set) > 1
        has_canonical = any(h.kind == CANONICAL for _, h in occ)

        # 跨 cue 形式汇总（上下文展示用）
        forms: dict[str, dict] = {}
        for ci, h in occ:
            f = forms.setdefault(h.surface, {
                "surface": h.surface,
                "kind": h.kind,
                "kind_label": TYPE_LABELS.get(h.kind, h.kind),
                "count": 0,
                "cue_seqs": [],
            })
            f["count"] += 1
            if ci not in f["cue_seqs"]:
                f["cue_seqs"].append(ci)

        def add(ci: int, h: _Hit, issue_type: str):
            cue = cues[ci]
            before, surface, after = _context(cue.text, h.begin, h.end)
            reason = _reason(issue_type, term, h.surface, cross_cue,
                             has_canonical)
            findings.append({
                "term_id": term.id,
                "language": term.language if term.language != "any"
                else cue_langs[ci],
                "canonical": term.canonical,
                "surface": h.surface,
                "issue_type": issue_type,
                "issue_type_label": TYPE_LABELS[issue_type],
                "severity": TYPE_SEVERITY[issue_type],
                "suggestion": term.canonical,
                "replacement": term.canonical,
                "cue_seq": ci,
                "start": round(cue.start, 3),
                "end": round(cue.end, 3),
                "begin_offset": h.begin,
                "end_offset": h.end,
                "context_before": before,
                "context_after": after,
                "cue_text": cue.text,
                "cross_cue": cross_cue,
                "forms": sorted(forms.values(),
                                key=lambda x: x["surface"]),
                "note": term.note,
                "reason": reason,
            })
            by_type[issue_type] = by_type.get(issue_type, 0) + 1

        for ci, h in occ:
            if h.kind in (FORBIDDEN, CASE_MISMATCH, WIDTH_MISMATCH,
                          PUNCT_MISMATCH, FUZZY):
                add(ci, h, h.kind)
            elif h.kind == VARIANT and has_canonical and cross_cue:
                # 规范词在其他 cue 出现：允许变体本身合规，但提示统一
                add(ci, h, VARIANT_MIXED)

    findings.sort(key=lambda f: (f["canonical"], f["cue_seq"],
                                 f["begin_offset"]))
    stats = {
        "term_count": len(terms),
        "terms_matched": sum(1 for t in terms if term_hits[id(t)]),
        "finding_count": len(findings),
        "by_type": by_type,
        "pending": len(findings),
        "accepted": 0,
        "rejected": 0,
    }
    return {"findings": findings, "stats": stats}


def _reason(issue_type: str, term: GlossaryTerm, surface: str,
            cross_cue: bool, has_canonical: bool) -> str:
    scope = "跨 cue " if cross_cue else ""
    if issue_type == FORBIDDEN:
        return (f"「{surface}」是术语「{term.canonical}」的禁止替代词，"
                f"应统一替换为规范词。")
    if issue_type == CASE_MISMATCH:
        return (f"{scope}大小写不一致：「{surface}」与规范词"
                f"「{term.canonical}」仅大小写不同。")
    if issue_type == WIDTH_MISMATCH:
        return (f"{scope}全半角不一致：「{surface}」与规范词"
                f"「{term.canonical}」仅全/半角形态不同。")
    if issue_type == PUNCT_MISMATCH:
        return (f"{scope}标点/空白差异：「{surface}」折叠标点空白后与规范词"
                f"「{term.canonical}」相同。")
    if issue_type == FUZZY:
        return (f"「{surface}」与规范词「{term.canonical}」拼写高度近似，"
                "疑似错别字。")
    if issue_type == VARIANT_MIXED:
        return (f"部分 cue 使用规范词「{term.canonical}」，本 cue 使用允许变体"
                f"「{surface}」，可统一为规范词。")
    return ""


# --- 决策应用 ----------------------------------------------------------------

def apply_replacements(cues: list[Cue], decisions: list[dict]) -> list[Cue]:
    """在原始 cue 文本上应用已采纳替换，时间轴保持不变。

    decisions: [{cue_seq, begin_offset, end_offset, replacement}]
    同一 cue 内的替换先按原文本坐标去重：偏移非法或与已选项重叠的条目
    被丢弃；之后按 begin 倒序应用，避免替换改变后续偏移。
    """
    by_cue: dict[int, list[dict]] = {}
    for d in decisions:
        by_cue.setdefault(int(d["cue_seq"]), []).append(d)

    revised: list[Cue] = []
    for ci, cue in enumerate(cues):
        edits = sorted(by_cue.get(ci, []),
                       key=lambda d: int(d["begin_offset"]))
        valid: list[dict] = []
        for d in edits:
            b, e = int(d["begin_offset"]), int(d["end_offset"])
            if not (0 <= b < e <= len(cue.text)):
                continue
            if any(b < ve and e > vb for vb, ve in
                   ((int(x["begin_offset"]), int(x["end_offset"]))
                    for x in valid)):
                continue  # 与已选替换重叠，跳过
            valid.append(d)

        text = cue.text
        for d in sorted(valid, key=lambda d: int(d["begin_offset"]),
                        reverse=True):
            b, e = int(d["begin_offset"]), int(d["end_offset"])
            text = text[:b] + str(d.get("replacement") or "") + text[e:]
        revised.append(Cue(start=cue.start, end=cue.end, text=text))
    return revised
