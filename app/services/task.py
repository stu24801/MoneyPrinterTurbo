import json
import math
import os.path
import re
from os import path

from loguru import logger

from app.config import config
from app.models import const
from app.models.schema import MaterialInfo, VideoConcatMode, VideoParams
from app.services import llm, material, subtitle, video, voice
from app.services import jobs
from app.services import state as sm
from app.utils import utils
from app.models.schema import VideoAspect


# ── storyboard.json raw I/O (self-contained for background job threads) ────────
def _sb_path(task_id):
    return path.join(utils.task_dir(task_id), "storyboard.json")


def _load_sb(task_id):
    try:
        with open(_sb_path(task_id), "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            data = {"style": "", "characters": [], "segments": data, "stage": "board"}
        return data
    except Exception:
        return {"style": "", "characters": [], "segments": [], "stage": "board"}


def _save_sb(task_id, data):
    tmp = _sb_path(task_id) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _sb_path(task_id))


def _seg_sig(s):
    import hashlib
    return hashlib.md5(
        ((s.get("script_chunk") or "") + "|" + (s.get("dialogue_text") or "") + "|"
         + (s.get("clip") or s.get("image") or "")).encode("utf-8")).hexdigest()[:12]


def _aspect_value(params):
    """params.video_aspect may be a VideoAspect enum or a raw string ('9:16')."""
    a = params.video_aspect
    return a.value if hasattr(a, "value") else a


def _seg_appearance(seg, characters):
    """該段對白中出現的角色外型描述（fallback: 全部角色），注入 Veo 影片/
    圖片 prompt，讓生成的人物嚴格符合角色設定參照、維持長相一致。"""
    if not characters:
        return ""
    names = {ln["speaker"] for ln in voice.parse_dialogue_lines(
        seg.get("dialogue_text", "") or "") if ln.get("speaker")}
    present = [c for c in characters if c.get("name") in names] or characters
    return "；".join(f"{c.get('name','')}：{c.get('appearance','')}"
                     for c in present if c.get("appearance"))


def _seg_beat(seg, which="last"):
    """該段開頭(first)/結尾(last)的演出情境（角色＋情緒＋台詞），供串場承接
    上一段的延續、接軌下一段的開場，讓轉場不突兀。無對白時退回場景描述。"""
    seg = seg or {}
    lines = voice.parse_dialogue_lines(seg.get("dialogue_text", "") or "")
    if not lines:
        return (seg.get("scene") or seg.get("video_prompt") or "").strip()[:60]
    ln = lines[-1] if which == "last" else lines[0]
    spk, emo, t = ln.get("speaker", ""), ln.get("emotion", ""), ln.get("line", "")
    if spk:
        return f"{spk}（{emo}）：{t}"
    return t


def _bridge_appearance(seg, next_seg, characters):
    """串場涉及的角色外型（前後兩段對白中出現的角色，fallback 全部角色），
    讓轉場畫面人物與前後段落設定一致。"""
    if not characters:
        return ""
    names = set()
    for txt in (seg.get("dialogue_text", ""), (next_seg or {}).get("dialogue_text", "")):
        for ln in voice.parse_dialogue_lines(txt or ""):
            if ln.get("speaker"):
                names.add(ln["speaker"])
    present = [c for c in characters if c.get("name") in names] or characters
    return "；".join(f"{c.get('name','')}：{c.get('appearance','')}"
                     for c in present if c.get("appearance"))


def drama_video_prompt(seg):
    """Build a Veo prompt that makes the character PERFORM the dialogue (mouth
    moving, expression & gesture matching the lines and emotion), so the acting
    matches the spoken dialogue instead of being a mute scene."""
    base = (seg.get("video_prompt") or seg.get("scene") or "").strip()
    dialogue = (seg.get("dialogue_text") or "").strip()
    if not dialogue:
        return base or "cinematic scene"
    lines = voice.parse_dialogue_lines(dialogue)
    perf = "；".join(
        f"{ln['speaker']}（{ln['emotion']}）說：「{ln['line']}」" if ln.get("speaker")
        else ln["line"] for ln in lines if ln.get("line"))
    scene = (base + "。") if base else ""
    # 對應的 25 格劇情分鏡格：把該格編號與劇情描述帶進提示，讓構圖／情節
    # 與對應分鏡格一致（board_image 已作為視覺錨點，這裡再以文字強化）。
    # 劇情要點：把該段劇情以「有序要點」帶入讓 Veo 有效參照。注意：用「劇情要點」
    # 而非「分鏡格/storyboard cell」，否則 Veo 會把畫面畫成多格分鏡表（分格首幀）。
    board = ""
    _cells = seg.get("board_cells") or []
    _beats = [b for b in (seg.get("board_beats") or []) if b]
    _cell = seg.get("board_cell")
    if _beats:
        _detail = "、".join(f"{i + 1}.「{b}」" for i, b in enumerate(_beats))
        board = (f"本段依序演出以下劇情要點：{_detail}。以單一連續的實拍電影鏡頭呈現，"
                 f"畫面禁止出現分鏡表、多格/九宮格、分割畫面或格線邊框。")
    elif _cell is not None:
        board = "以單一連續的實拍電影鏡頭呈現，畫面禁止出現分鏡表、多格拼貼或分割畫面。"
    # 對應的 9 鏡位環境美術構圖：把該格編號、鏡位與場景描述帶進提示，
    # 讓場景／取景與對應環境美術構圖一致。
    art = ""
    _acell = seg.get("art_cell")
    _ashot = seg.get("art_shot") or {}
    _aang = (_ashot.get("angle") or "").strip()
    _aprompt = (_ashot.get("prompt") or "").strip()
    if _aang or _aprompt:
        _adesc = "，".join([x for x in (_aang, _aprompt) if x])
        art = f"環境、取景與光線：{_adesc}。"
    return (f"{scene}{board}{art}角色對嘴說出以下台詞並以對應情緒表演"
            f"（嘴型、表情、肢體動作要吻合說話內容）：{perf}")


def _seg_character_refs(seg, characters, limit=4):
    """Model-sheet paths for the characters SPEAKING in this segment (fallback: all),
    so a per-segment still can lock exactly those characters to their finalized
    design — not just the first character's sheet (which left other characters
    off-model)."""
    names = {ln["speaker"] for ln in voice.parse_dialogue_lines(
        seg.get("dialogue_text", "") or "") if ln.get("speaker")}
    present = [c for c in (characters or []) if c.get("name") in names] or (characters or [])
    return [c["ref_image"] for c in present
            if c.get("ref_image") and os.path.exists(c["ref_image"])][:limit]


def drama_still_prompt(seg):
    """A STILL (first-frame) description of the segment's scene with its characters.
    IMPORTANT: describe the plot beats as SCENE CONTENT only — never as 'storyboard
    cells / 分鏡格 / grid', or the image model draws a multi-panel storyboard sheet
    (which then becomes a paneled first frame). Force a single cinematic frame."""
    parts = [x for x in (seg.get("scene", ""), seg.get("video_prompt", "")) if x]
    base = "。".join(parts)
    beats = [b for b in (seg.get("board_beats") or []) if b]
    if beats:
        base += "。畫面內容：" + "，".join(beats)
    return ("一張單一鏡頭、實拍電影感的完整畫面：" + (base or "cinematic scene")
            + "。人物長相須嚴格符合角色設定圖。"
            + "務必是單一連續的實拍畫面，禁止分鏡表、禁止多格/九宮格/漫畫分格、"
              "禁止分割畫面或畫面內出現格線邊框。")


def _drama_scene_still(task_id, aspect_value, seg, characters, style):
    """Generate a per-segment scene STILL with the segment's characters locked to
    their model sheets (reference_images), so the video's first frame is the SCENE
    (not a neutral model sheet) and the characters stay on their finalized design.
    Returns the still path or ""."""
    refs = _seg_character_refs(seg, characters)
    uid = seg.get("uid", 0)
    return material.generate_single_image_llm(
        task_id, drama_still_prompt(seg), VideoAspect(aspect_value),
        index=f"still-{uid}", style=style, reference_images=refs,
        appearance=_seg_appearance(seg, characters), out_name=f"seg-still-{uid}.png")


# ── Background job orchestration (run inside jobs.submit threads) ──────────────
def job_generate_image(task_id, uid, prompt, aspect_value, style, appearance, ref_images):
    """Generate one segment's storyboard image and persist it by uid."""
    aspect = VideoAspect(aspect_value)
    img = material.generate_single_image_llm(
        task_id, prompt, aspect, index=uid, style=style,
        reference_images=ref_images or [], appearance=appearance or "")
    if not img:
        raise RuntimeError("image generation returned empty")
    data = _load_sb(task_id)
    for s in data.get("segments", []):
        if s.get("uid") == uid:
            s["image"] = img
            old = s.get("clip", "")
            if old and old.endswith(".png.mp4") and os.path.exists(old):
                try:
                    os.remove(old)
                except OSError:
                    pass
                s["clip"] = ""
            break
    _save_sb(task_id, data)
    return {"image": img, "uid": uid}


def job_generate_clip(task_id, uid, prompt, aspect_value, max_dur, style, reference_image):
    """Generate one segment's Veo clip (image-to-video if reference_image) by uid."""
    aspect = VideoAspect(aspect_value)
    _note = {}
    # 戲劇模式：帶入該段角色外型，讓生成人物符合設定參照（尤其圖被拒改
    # text-to-video 時，無參考圖全靠文字，更需要外型描述維持一致）
    _sb = _load_sb(task_id)
    characters = _sb.get("characters", [])
    _seg = next((x for x in _sb.get("segments", []) if x.get("uid") == uid), {})
    _appear = _seg_appearance(_seg, characters)
    # 戲劇模式且未帶參考圖 → 先產「該場景＋定稿人物」分鏡靜圖當首幀（鎖定角色樣板，
    # 避免影片首幀變成中性樣板圖、且人物維持定稿長相）。
    _ref = reference_image or ""
    _still = ""
    _is_drama_seg = bool(characters) or bool((_seg.get("dialogue_text") or "").strip())
    if not _ref and _is_drama_seg:
        _still = _drama_scene_still(task_id, aspect_value, _seg, characters, style)
        _ref = _still or ""
    vid = material.generate_single_video_llm(
        task_id, prompt, aspect, max_clip_duration=max_dur, index=uid,
        style=style, reference_image=_ref, note_out=_note,
        appearance=_appear)
    if not vid:
        raise RuntimeError("video generation returned empty")
    data = _load_sb(task_id)
    for s in data.get("segments", []):
        if s.get("uid") == uid:
            s["clip"] = vid
            s["video_prompt"] = prompt
            if _still:
                s["still"] = _still
            s["motion_note"] = _note.get("motion", "")
            break
    _save_sb(task_id, data)
    return {"clip": vid, "uid": uid}


def job_generate_clips_batch(task_id, params: VideoParams, indices, style):
    """Regenerate only the Veo MATERIAL clip (not the composed segment video) for
    each selected segment, in ONE background job — so multiple segments can be
    re-generated with a single click instead of one-at-a-time. Drama segments get a
    fresh scene still (characters locked to their sheets) as the first frame."""
    total = len(indices)
    done = 0
    _sb0 = _load_sb(task_id)
    characters = _sb0.get("characters", [])
    for pos, idx in enumerate(indices):
        data = _load_sb(task_id)
        segs = data.get("segments", [])
        if not (0 <= idx < len(segs)):
            continue
        s = segs[idx]
        jobs.update_progress(task_id, "batch", pos, total, f"segment {idx + 1} · still")
        _still = _drama_scene_still(task_id, _aspect_value(params), s, characters, style)
        jobs.update_progress(task_id, "batch", pos, total, f"segment {idx + 1} · clip")
        vdir = drama_video_prompt(s) if (s.get("dialogue_text") or "").strip() \
            else (s.get("video_prompt") or s.get("scene") or "cinematic scene")
        _seg_dur = int(s.get("duration") or params.video_clip_duration or 6)
        _note = {}
        vid = material.generate_single_video_llm(
            task_id, vdir, VideoAspect(_aspect_value(params)),
            max_clip_duration=_seg_dur, index=s.get("uid", idx), style=style,
            reference_image=_still or "", note_out=_note,
            appearance=_seg_appearance(s, characters))
        if vid:
            data = _load_sb(task_id)
            segs = data.get("segments", [])
            segs[idx]["clip"] = vid
            segs[idx]["still"] = _still or ""
            segs[idx]["motion_note"] = _note.get("motion", "")
            # clip 換了 → 舊的合成段落影片過期，清掉讓後續 Render 會重新合成
            segs[idx].pop("segment_video", None)
            segs[idx].pop("rendered_sig", None)
            _save_sb(task_id, data)
            done += 1
        jobs.update_progress(task_id, "batch", pos + 1, total, f"segment {idx + 1}")
    return {"clips": done, "total": total}


def _script_text(task_id):
    """Read the original script for this task (for whole-story pre-production)."""
    try:
        with open(path.join(utils.task_dir(task_id), "script.json"), "r", encoding="utf-8") as f:
            return (json.load(f) or {}).get("script", "") or ""
    except Exception:
        return ""


def _char_ref_paths(characters, limit=4):
    """Existing character reference portrait paths (model sheets)."""
    return [c["ref_image"] for c in (characters or [])
            if c.get("ref_image") and os.path.exists(c["ref_image"])][:limit]


def _all_appearance(characters):
    return "；".join(f"{c.get('name','')}：{c.get('appearance','')}"
                     for c in (characters or []) if c.get("appearance"))


def job_generate_plot_board(task_id, params: VideoParams, style, suggestions="", n_cells=25):
    """Whole-story 25-cell (5x5) plot storyboard image. Replaces per-segment boards.
    `suggestions` steers a regeneration. Persists plot_board and advances stage."""
    data = _load_sb(task_id)
    characters = data.get("characters", [])
    script = _script_text(task_id)
    res = llm.generate_plot_board(script, characters, style=style,
                                  n_cells=n_cells, suggestions=suggestions)
    image_prompt = res.get("image_prompt") or "A 5x5 storyboard grid of the story"
    img = material.generate_single_image_llm(
        task_id, image_prompt, VideoAspect(_aspect_value(params)), style=style,
        reference_images=_char_ref_paths(characters), appearance=_all_appearance(characters),
        out_name="plot-board.png")
    if not img:
        raise RuntimeError("plot board image generation returned empty")
    import hashlib
    data = _load_sb(task_id)
    data["plot_board"] = {
        "image": img, "suggestions": suggestions, "beats": res.get("beats", []),
        "sig": hashlib.md5((image_prompt + suggestions).encode("utf-8")).hexdigest()[:12]}
    if data.get("stage") in (None, "", "cast", "board"):
        data["stage"] = "plotboard"
    _save_sb(task_id, data)
    return {"image": img}


def _crop_plot_cell(task_id, plot_image, cell_index, out_name, rows=5, cols=5):
    """Crop one cell (reading order, 0-based) from a rows×cols grid storyboard
    sheet, so a segment can show / anchor on ITS corresponding board panel.
    Returns the saved crop path or ""."""
    try:
        from PIL import Image
        if not (plot_image and os.path.exists(plot_image)):
            return ""
        im = Image.open(plot_image).convert("RGB")
        w, h = im.size
        cell_index = max(0, min(rows * cols - 1, int(cell_index)))
        r, c = cell_index // cols, cell_index % cols
        box = (int(c * w / cols), int(r * h / rows),
               int((c + 1) * w / cols), int((r + 1) * h / rows))
        out = os.path.join(utils.task_dir(task_id), out_name)
        im.crop(box).save(out)
        return out if os.path.exists(out) else ""
    except Exception as e:
        logger.warning(f"crop plot cell failed: {e}")
        return ""


def job_generate_art_shots(task_id, params: VideoParams, style, n=9):
    """Environment / camera-angle art board: ONE image split into a 3x3 grid of 9
    numbered environment compositions (derived from the plot board & cast).
    Persists art_shots (the 9 angle/prompt descriptions) + art_board {image}."""
    data = _load_sb(task_id)
    characters = data.get("characters", [])
    script = _script_text(task_id)
    shots = llm.generate_art_shots(script, characters, style=style, n=n)
    refs = _char_ref_paths(characters, limit=2)
    plot_img = (data.get("plot_board") or {}).get("image", "")
    ref_all = ([plot_img] if plot_img and os.path.exists(plot_img) else []) + refs
    panel_lines = "; ".join(f"Panel {i + 1} ({s.get('angle', '')}): {s.get('prompt', '')}"
                            for i, s in enumerate(shots) if s.get("prompt"))
    style_part = f" Visual style: {style.strip()}." if (style or "").strip() else ""
    grid_prompt = (
        f"A single art-direction sheet: one 3x3 grid of 9 numbered environment "
        f"concept panels (establishing shots / camera set-ups), each panel clearly "
        f"framed and labeled 1-9, read left-to-right and top-to-bottom. {panel_lines}."
        f"{style_part} Coherent color palette and lighting across all panels. "
        f"No characters foregrounded, no captions besides the panel numbers."
    )
    img = material.generate_single_image_llm(
        task_id, grid_prompt, VideoAspect(_aspect_value(params)),
        style=style, reference_images=ref_all[:4], out_name="art-board.png")
    data = _load_sb(task_id)
    data["art_shots"] = [{"angle": s.get("angle", ""), "prompt": s.get("prompt", "")} for s in shots]
    data["art_board"] = {"image": img or ""}
    if data.get("stage") in (None, "", "plotboard"):
        data["stage"] = "artshots"
    _save_sb(task_id, data)
    if not img:
        raise RuntimeError("art board image generation returned empty")
    return {"image": img}


def cell_group(row_index, n_rows, n_cells):
    """Deterministic mapping of a text-board row → its contiguous group of plot-board
    cells (reading order). Matches generate_text_board's 'cover beats IN ORDER'
    constraint. Returns (lo, hi, mid) as 0-based cell indices, hi exclusive; the
    display cell numbers are lo+1 .. hi (1-based)."""
    n_rows = max(1, int(n_rows))
    n_cells = max(1, int(n_cells))
    row_index = max(0, min(n_rows - 1, int(row_index)))
    lo = row_index * n_cells // n_rows
    hi = max(lo + 1, (row_index + 1) * n_cells // n_rows)
    mid = min(n_cells - 1, (lo + hi - 1) // 2)
    return lo, hi, mid


def _annotate_text_board_cells(rows, n_cells, n_art=9):
    """Attach the corresponding plot-board cell numbering (25-grid) AND environment
    art-shot cell numbering (9-grid) onto each text-board row. plot cells:
    cells (1-based list), cell_lo/cell_hi (0-based range), cell_mid (0-based rep);
    art: art_cells (1-based list), art_cell (0-based rep)."""
    n_cells = max(1, int(n_cells))
    n_art = max(1, int(n_art))
    n = max(1, len(rows))
    for i, r in enumerate(rows):
        if not isinstance(r, dict):
            continue
        lo, hi, mid = cell_group(i, n, n_cells)
        r["cell_lo"] = lo
        r["cell_hi"] = hi
        r["cell_mid"] = mid
        r["cells"] = list(range(lo + 1, hi + 1))  # 1-based plot cell numbers
        alo, ahi, amid = cell_group(i, n, n_art)
        r["art_cell"] = amid
        r["art_cells"] = list(range(alo + 1, ahi + 1))  # 1-based art panel numbers
    return rows


def job_generate_text_board(task_id, params: VideoParams, style, n_segments=0,
                            batch_size=2):
    """Detailed textual storyboard (per-segment scene / emotion / dialogue / action),
    synthesized from the plot beats + art shots + character designs. Persists
    text_board and advances stage; the UI's '切版' step cuts it into segments.
    Each row is annotated with its corresponding 25-cell plot-board cell numbers.

    Generated in SMALL BATCHES (default 2 segments/call) rather than one giant call:
    keeps each LLM request small so it returns fast and the JSON parses reliably
    (the whole-board single call was slow and kept timing out / failing to parse).
    Progress is reported per batch so the UI shows real progress, not a frozen bar."""
    data = _load_sb(task_id)
    characters = data.get("characters", [])
    script = _script_text(task_id)
    beats = (data.get("plot_board") or {}).get("beats", []) or []
    art = data.get("art_shots", []) or []
    n = int(n_segments) or len(data.get("segments", [])) or 5
    n_cells = len(beats) or 25
    n_art = len(art) or 9
    b = max(1, int(batch_size))

    rows = []
    for start in range(0, n, b):
        count = min(b, n - start)
        # 這批各段對應的劇情 beats 與環境美術（依 cell_group 連續分組，維持標號一致）
        seg_beats, seg_art = [], []
        for j in range(count):
            i = start + j
            _lo, _hi, _ = cell_group(i, n, n_cells)
            seg_beats.append([x for x in beats[_lo:_hi] if x])
            _am = cell_group(i, n, n_art)[2]
            _sh = art[_am] if 0 <= _am < len(art) else {}
            seg_art.append(f"{_sh.get('angle', '')}：{_sh.get('prompt', '')}".strip("："))
        prev_scene = rows[-1].get("scene", "") if rows else ""
        chunk = llm.generate_text_board_chunk(
            script, characters, style, start, count, n,
            seg_beats, seg_art, prev_scene=prev_scene)
        rows.extend(chunk)
        # 逐批存檔並回報進度（heartbeat）→ 前端可見進度、失敗也保留已完成的段
        _partial = _annotate_text_board_cells(list(rows), n_cells, n_art)
        d = _load_sb(task_id)
        d["text_board"] = _partial
        _save_sb(task_id, d)
        jobs.update_progress(task_id, "textboard", min(start + count, n), n,
                             f"segments {start + 1}-{start + count}/{n}")

    _annotate_text_board_cells(rows, n_cells, n_art)
    data = _load_sb(task_id)
    data["text_board"] = rows
    if data.get("stage") in (None, "", "artshots"):
        data["stage"] = "textboard"
    _save_sb(task_id, data)
    return {"rows": len(rows)}


_CJK_CPS = 4.0  # 中文旁白/台詞語速 ~4 字/秒（與 script 時長估算一致）


def estimate_speech_seconds(dialogue_text, cps=_CJK_CPS):
    """Rough spoken duration of a drama dialogue block: sum of the spoken 台詞
    lengths / chars-per-second (CJK ~4). Only the line content counts (not the
    角色名（情感） prefix)."""
    lines = voice.parse_dialogue_lines(dialogue_text or "")
    chars = sum(len((ln.get("line") or "").strip()) for ln in lines)
    if not chars:
        chars = len((dialogue_text or "").strip())
    return chars / max(1.0, cps)


def _split_line_by_punct(text, max_chars):
    """Split one over-long utterance into <=max_chars pieces at sentence
    punctuation (fallback: hard slice)."""
    import re as _re
    parts, buf = [], ""
    for tok in _re.findall(r"[^。！？；;!?，,]*[。！？；;!?，,]?", text):
        if not tok:
            continue
        if buf and len(buf) + len(tok) > max_chars:
            parts.append(buf)
            buf = tok
        else:
            buf += tok
    if buf:
        parts.append(buf)
    out = []
    for p in parts:
        while len(p) > max_chars:
            out.append(p[:max_chars])
            p = p[max_chars:]
        if p:
            out.append(p)
    return out or [text]


def split_dialogue_by_duration(dialogue_text, max_seconds=8, cps=_CJK_CPS):
    """Split a dialogue block into contiguous chunks whose spoken duration each
    fits within max_seconds, so a segment's script never exceeds the producible
    video length. Splits at utterance boundaries (角色名（情感）：台詞); an utterance
    longer than the budget is further split at sentence punctuation. Returns a
    list of dialogue-text strings (>=1, preserving 角色名（情感）：台詞 format)."""
    max_chars = max(1, int(max_seconds * cps))
    lines = voice.parse_dialogue_lines(dialogue_text or "")
    if not lines:
        return [dialogue_text or ""]
    units = []  # (speaker, emotion, line)
    for ln in lines:
        spk, emo, txt = ln.get("speaker", ""), ln.get("emotion", ""), (ln.get("line") or "").strip()
        if not txt:
            continue
        if len(txt) > max_chars:
            for piece in _split_line_by_punct(txt, max_chars):
                units.append((spk, emo, piece))
        else:
            units.append((spk, emo, txt))
    if not units:
        return [dialogue_text or ""]
    chunks, cur, cur_chars = [], [], 0
    for spk, emo, txt in units:
        if cur and cur_chars + len(txt) > max_chars:
            chunks.append(cur)
            cur, cur_chars = [], 0
        cur.append((spk, emo, txt))
        cur_chars += len(txt)
    if cur:
        chunks.append(cur)

    def _fmt(spk, emo, txt):
        if spk and emo:
            return f"{spk}（{emo}）：{txt}"
        if spk:
            return f"{spk}：{txt}"
        return txt
    return ["\n".join(_fmt(*u) for u in ch) for ch in chunks] or [dialogue_text or ""]


def snap_duration(need_seconds, choices=(4, 6, 8)):
    """Smallest allowed Veo clip length (s) that covers need_seconds (cap = max)."""
    return next((d for d in choices if d >= need_seconds), choices[-1])


def purge_segment_media(task_id, seg, drop=False):
    """Remove a segment's previously generated media so a re-run regenerates fresh
    instead of reusing stale artifacts: the Veo clip (llm-video-{uid}.mp4), the
    rendered segment video, and the bridge clip + its audio/subtitle. Only files
    inside the task dir are removed. Clears the corresponding fields on seg unless
    drop=True (the segment is being removed entirely)."""
    tdir = os.path.abspath(utils.task_dir(task_id))
    uid = seg.get("uid")
    targets = [seg.get("clip"), seg.get("segment_video"), seg.get("bridge_clip"),
               seg.get("still")]
    if uid is not None:
        targets += [os.path.join(tdir, n) for n in (
            f"llm-video-{uid}.mp4", f"seg-still-{uid}.png", f"bridge-{uid}.mp4",
            f"bridge-{uid}-audio.mp3", f"bridge-{uid}.srt", f"bridge-{uid}-sub.mp3")]
    for p in targets:
        try:
            if p and os.path.isfile(p) and os.path.abspath(p).startswith(tdir):
                os.remove(p)
        except OSError:
            pass
    if not drop:
        for k in ("clip", "segment_video", "bridge_clip", "bridge_narration",
                  "still", "rendered_sig", "motion_note"):
            seg.pop(k, None)


def job_render_segments(task_id, params: VideoParams, voice_map, seg_inputs,
                        auto_motion=False, voice_mode="tts"):
    """Render the given segment inputs, updating segment_video by index and
    setting stage=segments. When auto_motion is True, any segment that only has
    a static image (no Veo clip) first gets a Veo image-to-video clip generated
    from its storyboard image + video direction, so the result feels dramatic
    rather than a static zoom."""
    total = len(seg_inputs)
    rendered = 0
    _sb0 = _load_sb(task_id)
    style = _sb0.get("style", "")
    characters = _sb0.get("characters", [])
    _drama = getattr(params, "presentation_mode", "narration") == "drama" or bool(characters)
    for pos, inp in enumerate(seg_inputs):
        idx = int(inp.get("index", pos))
        data = _load_sb(task_id)
        segs = data.get("segments", [])
        # 自愈：段落沒有分鏡圖也沒有影片 → 先自動補素材（否則無法渲染）
        if 0 <= idx < len(segs):
            s = segs[idx]
            _clip = s.get("clip") or ""
            _image = s.get("image") or ""
            _has_clip = _clip and os.path.exists(_clip)
            _has_image = _image and os.path.exists(_image)
            if not _has_clip and not _has_image and _drama:
                # 戲劇新流程：段落分鏡圖已由 25 格劇情分鏡圖取代，不再產靜圖。
                # 直接產出「只出台詞對嘴、無人聲」的 Veo 影片底稿（人聲後續配音補上）。
                jobs.update_progress(task_id, "batch", pos, total, f"segment {idx + 1} · still")
                vdir = drama_video_prompt(s) if (s.get("dialogue_text") or "").strip() \
                    else (s.get("video_prompt") or s.get("scene") or "cinematic scene")
                _seg_dur = int(s.get("duration") or params.video_clip_duration or 6)
                # 先用「該段說話角色的樣板圖」為參考，產出該場景的分鏡靜圖（人物鎖定
                # 定稿長相），再以此靜圖做 image-to-video → 影片首幀是場景（非中性樣板
                # 圖），且人物維持角色設定。修掉舊版只傳第一個角色樣板→其他角色跑掉。
                _still = _drama_scene_still(task_id, _aspect_value(params), s, characters, style)
                jobs.update_progress(task_id, "batch", pos, total, f"segment {idx + 1} · clip")
                _note = {}
                vid = material.generate_single_video_llm(
                    task_id, vdir, VideoAspect(_aspect_value(params)),
                    max_clip_duration=_seg_dur, index=s.get("uid", idx),
                    style=style, reference_image=_still or "",
                    note_out=_note, appearance=_seg_appearance(s, characters))
                if vid:
                    data = _load_sb(task_id)
                    segs = data.get("segments", [])
                    segs[idx]["still"] = _still or ""
                    segs[idx]["clip"] = vid
                    segs[idx]["motion_note"] = _note.get("motion", "")
                    inp["clip"] = vid
                    _save_sb(task_id, data)
            elif not _has_clip and not _has_image:
                jobs.update_progress(task_id, "batch", pos, total, f"segment {idx + 1} · image")
                _iprompt = (s.get("prompt") or s.get("video_prompt")
                            or s.get("scene") or s.get("dialogue_text")
                            or s.get("script_chunk") or "cinematic scene")
                _appear = _seg_appearance(s, characters)
                _img = material.generate_single_image_llm(
                    task_id, _iprompt, VideoAspect(_aspect_value(params)),
                    index=s.get("uid", idx), style=style, appearance=_appear)
                if _img:
                    data = _load_sb(task_id)
                    segs = data.get("segments", [])
                    segs[idx]["image"] = _img
                    inp["image"] = _img
                    _save_sb(task_id, data)
        # Auto-motion: image-only segment → generate a Veo clip first
        if auto_motion and 0 <= idx < len(segs):
            s = segs[idx]
            clip = s.get("clip") or ""
            image = s.get("image") or ""
            if (not clip or not os.path.exists(clip)) and image and os.path.exists(image):
                jobs.update_progress(task_id, "batch", pos, total, f"segment {idx + 1} · motion")
                # 戲劇模式：把台詞帶入 Veo prompt，讓角色演出對應台詞（對嘴/表情/動作）
                if (s.get("dialogue_text") or "").strip():
                    vdir = drama_video_prompt(s)
                else:
                    vdir = s.get("video_prompt") or s.get("script_chunk") or ""
                _seg_dur = int(s.get("duration") or params.video_clip_duration or 6)
                _note = {}
                vid = material.generate_single_video_llm(
                    task_id, vdir, VideoAspect(_aspect_value(params)),
                    max_clip_duration=_seg_dur, index=s.get("uid", idx),
                    style=style, reference_image=image, note_out=_note,
                    appearance=_seg_appearance(s, characters))
                if vid:
                    data = _load_sb(task_id)
                    segs = data.get("segments", [])
                    segs[idx]["clip"] = vid
                    segs[idx]["motion_note"] = _note.get("motion", "")
                    inp["clip"] = vid
                    _save_sb(task_id, data)
        outs = generate_segments(task_id, params, [inp], voice_map=voice_map, voice_mode=voice_mode)
        out = outs[0] if outs else ""
        data = _load_sb(task_id)
        segs = data.get("segments", [])
        if 0 <= idx < len(segs) and out:
            segs[idx]["segment_video"] = out
            segs[idx]["rendered_sig"] = _seg_sig(segs[idx])
            rendered += 1
            _save_sb(task_id, data)
        jobs.update_progress(task_id, "batch", pos + 1, total, f"segment {idx + 1}")
    data = _load_sb(task_id)
    data["stage"] = "segments"
    _save_sb(task_id, data)
    return {"rendered": rendered, "total": total}


def _generate_bridge(task_id, params, seg, idx, style, bridge_voice, narration=False,
                     characters=None, next_seg=None):
    """Generate a short bridging (串場) clip from a segment's connecting note
    (串接說明 / transition_note): a transitional Veo scene that stays consistent
    with the adjacent segments' characters/scene. When narration=True a brief
    voiceover + subtitle of the note is added; otherwise the bridge is just the
    transitional scene with its own ambient sound. Returns the path or ""."""
    note = (seg.get("transition_note") or "").strip()
    if not note:
        return ""
    task_dir = utils.task_dir(task_id)
    uid = seg.get("uid", idx)
    # 保持與上下段落一致：場景銜接 + 前段延續情境 + 後段接軌情境 + 角色外型 + 風格
    from_scene = (seg.get("scene") or seg.get("video_prompt") or "").strip()
    to_scene = ((next_seg or {}).get("scene") or (next_seg or {}).get("video_prompt") or "").strip()
    from_beat = _seg_beat(seg, "last")          # 上一段結尾情境（延續）
    to_beat = _seg_beat(next_seg, "first")      # 下一段開場情境（接軌）
    ctx_parts = []
    if from_scene or from_beat:
        ctx_parts.append(f"承接上一段的情境（場景：{from_scene}；剛發生：{from_beat}）")
    if to_scene or to_beat:
        ctx_parts.append(f"自然接軌到下一段（場景：{to_scene}；即將發生：{to_beat}）")
    scene_ctx = ("，".join(ctx_parts) + "。") if ctx_parts else ""
    # 角色外型走 generate_single_video_llm 的 appearance 參數（格式統一）
    appear = _bridge_appearance(seg, next_seg, characters)
    bridge_prompt = (f"電影感的轉場過渡畫面，銜接劇情：{note}。{scene_ctx}"
                     f"維持與前後段落一致的場景氛圍與人物，柔和運鏡，無文字")
    vclip = material.generate_single_video_llm(
        task_id, bridge_prompt, VideoAspect(_aspect_value(params)),
        max_clip_duration=4, index=f"bridge-{uid}", style=style,
        reference_image="", appearance=appear)
    if not vclip or not path.exists(vclip):
        return ""

    audio_f, srt_f = "", ""
    if narration:
        # 選用：串接說明的旁白 + 字幕
        audio_f = path.join(task_dir, f"bridge-{uid}-audio.mp3")
        if not voice.tts_emotion(text=note, voice_name=bridge_voice, emotion="平靜", out_file=audio_f):
            sm = voice.tts(text=note, voice_name=voice.parse_voice_name(bridge_voice),
                           voice_rate=1.0, voice_file=audio_f)
            if sm is None or not path.exists(audio_f):
                audio_f = ""
        if audio_f and params.subtitle_enabled:
            srt_f = path.join(task_dir, f"bridge-{uid}.srt")
            try:
                sm2 = voice.tts(text=note, voice_name=voice.parse_voice_name(bridge_voice),
                                voice_rate=1.0, voice_file=path.join(task_dir, f"bridge-{uid}-sub.mp3"))
                if sm2 is not None:
                    voice.create_subtitle(sub_maker=sm2, text=note, subtitle_file=srt_f)
            except Exception:
                srt_f = ""
            if not path.exists(srt_f):
                srt_f = ""

    # compose bridge — narration off → 只用畫面+原生環境音（audio_f 空）
    out = path.join(task_dir, f"bridge-{uid}.mp4")
    try:
        video.compose_segment_video(clip_path=vclip, audio_path=audio_f,
                                    subtitle_path=srt_f, output_file=out, params=params)
    except Exception as e:
        logger.error(f"bridge compose failed: {e}")
        return ""
    return out if path.exists(out) else ""


def job_merge(task_id, params: VideoParams, use_transitions=True, with_bridges=False,
              bridge_narration=False):
    """Merge all rendered segment videos into the final film. use_transitions=False
    → hard cuts (continuous). with_bridges=True → generate a short interstitial
    (串場) from each segment's connecting note and insert it between segments
    (the film gets longer, but the narrative flows more completely)."""
    data = _load_sb(task_id)
    segs = data.get("segments", [])
    bridge_voice = params.voice_name or "zh-TW-HsiaoChenNeural-Female"
    style = data.get("style", "")
    # 重新合併＝重新產製：先移除舊的成片，避免合併失敗時殘留舊檔被當成新結果
    _old_final = os.path.join(utils.task_dir(task_id), "final-1.mp4")
    try:
        if os.path.isfile(_old_final):
            os.remove(_old_final)
    except OSError:
        pass

    import hashlib

    def _bridge_sig(s, nxt):
        return hashlib.md5(
            ((s.get("transition_note") or "") + "|" + _seg_beat(s, "last") + "|"
             + _seg_beat(nxt, "first") + "|" + str(bool(bridge_narration))
             ).encode("utf-8")).hexdigest()[:12]

    ordered_files, ordered_fx = [], []
    for i, s in enumerate(segs):
        sv = s.get("segment_video")
        if not (sv and os.path.exists(sv)):
            continue
        e = s.get("transition_effect", "none")
        if use_transitions and e == "none" and (ordered_files):
            e = "fade_in"
        elif not use_transitions:
            e = "none"
        ordered_files.append(sv)
        ordered_fx.append(e)
        # 串場：段落之後（最後一段除外）插入依串接說明生成的過渡片段
        if with_bridges and i < len(segs) - 1:
            bclip = s.get("bridge_clip")
            # 旁白模式或前後段內容變更時，已快取的串場要重新產製
            _sig = _bridge_sig(s, segs[i + 1] if i + 1 < len(segs) else None)
            _stale = bool(bclip) and s.get("bridge_sig") != _sig
            if not (bclip and os.path.exists(bclip)) or _stale:
                jobs.update_progress(task_id, "merge", i, len(segs), f"bridge {i + 1}")
                bclip = _generate_bridge(task_id, params, s, i, style, bridge_voice,
                                         narration=bridge_narration,
                                         characters=data.get("characters", []),
                                         next_seg=segs[i + 1] if i + 1 < len(segs) else None)
                if bclip:
                    s["bridge_clip"] = bclip
                    s["bridge_narration"] = bool(bridge_narration)
                    s["bridge_sig"] = _sig
                    _save_sb(task_id, data)
            if bclip and os.path.exists(bclip):
                ordered_files.append(bclip)
                ordered_fx.append("fade_in" if use_transitions else "none")

    if not ordered_files:
        raise RuntimeError("no segment videos to merge")
    fin = merge_segments(task_id, params, ordered_files, transitions=ordered_fx)
    if not fin or not fin.get("videos"):
        raise RuntimeError("merge produced no video")
    return {"videos": fin["videos"]}


def generate_script(task_id, params):
    logger.info("\n\n## generating video script")
    video_script = params.video_script.strip()
    if not video_script:
        video_script = llm.generate_script(
            video_subject=params.video_subject,
            language=params.video_language,
            paragraph_number=params.paragraph_number,
            target_duration=getattr(params, "video_total_duration", 0) or 0,
        )
    else:
        logger.debug(f"video script: \n{video_script}")

    if not video_script:
        sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
        logger.error("failed to generate video script.")
        return None

    return video_script


def generate_terms(task_id, params, video_script):
    logger.info("\n\n## generating video terms")
    video_terms = params.video_terms
    if not video_terms:
        video_terms = llm.generate_terms(
            video_subject=params.video_subject, video_script=video_script, amount=5
        )
    else:
        if isinstance(video_terms, str):
            video_terms = [term.strip() for term in re.split(r"[,，]", video_terms)]
        elif isinstance(video_terms, list):
            video_terms = [term.strip() for term in video_terms]
        else:
            raise ValueError("video_terms must be a string or a list of strings.")

        logger.debug(f"video terms: {utils.to_json(video_terms)}")

    if not video_terms:
        sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
        logger.error("failed to generate video terms.")
        return None

    return video_terms


def save_script_data(task_id, video_script, video_terms, params):
    script_file = path.join(utils.task_dir(task_id), "script.json")
    script_data = {
        "script": video_script,
        "search_terms": video_terms,
        "params": params,
    }

    with open(script_file, "w", encoding="utf-8") as f:
        f.write(utils.to_json(script_data))


def generate_audio(task_id, params, video_script):
    '''
    Generate audio for the video script.
    If a custom audio file is provided, it will be used directly.
    There will be no subtitle maker object returned in this case.
    Otherwise, TTS will be used to generate the audio.
    Returns:
        - audio_file: path to the generated or provided audio file
        - audio_duration: duration of the audio in seconds
        - sub_maker: subtitle maker object if TTS is used, None otherwise
    '''
    logger.info("\n\n## generating audio")
    custom_audio_file = params.custom_audio_file
    if not custom_audio_file or not os.path.exists(custom_audio_file):
        if custom_audio_file:
            logger.warning(
                f"custom audio file not found: {custom_audio_file}, using TTS to generate audio."
            )
        else:
            logger.info("no custom audio file provided, using TTS to generate audio.")
        audio_file = path.join(utils.task_dir(task_id), "audio.mp3")
        sub_maker = voice.tts(
            text=video_script,
            voice_name=voice.parse_voice_name(params.voice_name),
            voice_rate=params.voice_rate,
            voice_file=audio_file,
        )
        if sub_maker is None:
            sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
            logger.error(
                """failed to generate audio:
1. check if the language of the voice matches the language of the video script.
2. check if the network is available. If you are in China, it is recommended to use a VPN and enable the global traffic mode.
            """.strip()
            )
            return None, None, None
        audio_duration = math.ceil(voice.get_audio_duration(sub_maker))
        if audio_duration == 0:
            sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
            logger.error("failed to get audio duration.")
            return None, None, None
        return audio_file, audio_duration, sub_maker
    else:
        logger.info(f"using custom audio file: {custom_audio_file}")
        audio_duration = voice.get_audio_duration(custom_audio_file)
        if audio_duration == 0:
            sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
            logger.error("failed to get audio duration from custom audio file.")
            return None, None, None
        return custom_audio_file, audio_duration, None

def generate_subtitle(task_id, params, video_script, sub_maker, audio_file):
    '''
    Generate subtitle for the video script.
    If subtitle generation is disabled or no subtitle maker is provided, it will return an empty string.
    Otherwise, it will generate the subtitle using the specified provider.
    Returns:
        - subtitle_path: path to the generated subtitle file
    '''
    logger.info("\n\n## generating subtitle")
    if not params.subtitle_enabled or sub_maker is None:
        return ""

    subtitle_path = path.join(utils.task_dir(task_id), "subtitle.srt")
    subtitle_provider = config.app.get("subtitle_provider", "edge").strip().lower()
    logger.info(f"\n\n## generating subtitle, provider: {subtitle_provider}")

    subtitle_fallback = False
    if subtitle_provider == "edge":
        voice.create_subtitle(
            text=video_script, sub_maker=sub_maker, subtitle_file=subtitle_path
        )
        if not os.path.exists(subtitle_path):
            subtitle_fallback = True
            logger.warning("subtitle file not found, fallback to whisper")

    if subtitle_provider == "whisper" or subtitle_fallback:
        subtitle.create(audio_file=audio_file, subtitle_file=subtitle_path)
        logger.info("\n\n## correcting subtitle")
        subtitle.correct(subtitle_file=subtitle_path, video_script=video_script)

    subtitle_lines = subtitle.file_to_subtitles(subtitle_path)
    if not subtitle_lines:
        logger.warning(f"subtitle file is invalid: {subtitle_path}")
        return ""

    return subtitle_path


def get_video_materials(task_id, params, video_terms, audio_duration):
    if params.video_source == "llm-video":
        logger.info("\n\n## generating video clips via Veo (LLM proxy video model)")
        materials = material.generate_videos_llm(
            task_id=task_id,
            search_terms=video_terms,
            video_aspect=params.video_aspect,
            audio_duration=audio_duration * params.video_count,
            max_clip_duration=params.video_clip_duration,
        )
        if not materials:
            sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
            logger.error("failed to generate video clips via Veo.")
            return None
        # Veo clips are already video files — no image preprocessing needed
        return [material_info.url for material_info in materials]
    if params.video_source == "llm":
        logger.info("\n\n## generating image materials via LLM image model")
        materials = material.generate_images_llm(
            task_id=task_id,
            search_terms=video_terms,
            video_aspect=params.video_aspect,
            audio_duration=audio_duration * params.video_count,
            max_clip_duration=params.video_clip_duration,
        )
        if not materials:
            sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
            logger.error("failed to generate images via LLM image model.")
            return None
        materials = video.preprocess_video(
            materials=materials, clip_duration=params.video_clip_duration
        )
        if not materials:
            sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
            logger.error("no valid generated images after preprocessing.")
            return None
        return [material_info.url for material_info in materials]
    if params.video_source == "local":
        logger.info("\n\n## preprocess local materials")
        materials = video.preprocess_video(
            materials=params.video_materials, clip_duration=params.video_clip_duration
        )
        if not materials:
            sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
            logger.error(
                "no valid materials found, please check the materials and try again."
            )
            return None
        return [material_info.url for material_info in materials]
    else:
        logger.info(f"\n\n## downloading videos from {params.video_source}")
        downloaded_videos = material.download_videos(
            task_id=task_id,
            search_terms=video_terms,
            source=params.video_source,
            video_aspect=params.video_aspect,
            video_contact_mode=params.video_concat_mode,
            audio_duration=audio_duration * params.video_count,
            max_clip_duration=params.video_clip_duration,
        )
        if not downloaded_videos:
            sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
            logger.error(
                "failed to download videos, maybe the network is not available. if you are in China, please use a VPN."
            )
            return None
        return downloaded_videos


def generate_final_videos(
    task_id, params, downloaded_videos, audio_file, subtitle_path
):
    final_video_paths = []
    combined_video_paths = []
    video_concat_mode = (
        params.video_concat_mode if params.video_count == 1 else VideoConcatMode.random
    )
    video_transition_mode = params.video_transition_mode

    _progress = 50
    for i in range(params.video_count):
        index = i + 1
        combined_video_path = path.join(
            utils.task_dir(task_id), f"combined-{index}.mp4"
        )
        logger.info(f"\n\n## combining video: {index} => {combined_video_path}")
        video.combine_videos(
            combined_video_path=combined_video_path,
            video_paths=downloaded_videos,
            audio_file=audio_file,
            video_aspect=params.video_aspect,
            video_concat_mode=video_concat_mode,
            video_transition_mode=video_transition_mode,
            max_clip_duration=params.video_clip_duration,
            threads=params.n_threads,
        )

        _progress += 50 / params.video_count / 2
        sm.state.update_task(task_id, progress=_progress)

        final_video_path = path.join(utils.task_dir(task_id), f"final-{index}.mp4")

        logger.info(f"\n\n## generating video: {index} => {final_video_path}")
        video.generate_video(
            video_path=combined_video_path,
            audio_path=audio_file,
            subtitle_path=subtitle_path,
            output_file=final_video_path,
            params=params,
        )

        _progress += 50 / params.video_count / 2
        sm.state.update_task(task_id, progress=_progress)

        final_video_paths.append(final_video_path)
        combined_video_paths.append(combined_video_path)

    return final_video_paths, combined_video_paths


def start(task_id, params: VideoParams, stop_at: str = "video"):
    logger.info(f"start task: {task_id}, stop_at: {stop_at}")
    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=5)

    if type(params.video_concat_mode) is str:
        params.video_concat_mode = VideoConcatMode(params.video_concat_mode)

    # 1. Generate script
    video_script = generate_script(task_id, params)
    if not video_script or "Error: " in video_script:
        sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
        return

    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=10)

    if stop_at == "script":
        sm.state.update_task(
            task_id, state=const.TASK_STATE_COMPLETE, progress=100, script=video_script
        )
        return {"script": video_script}

    # 2. Generate terms
    video_terms = ""
    if params.video_source != "local":
        video_terms = generate_terms(task_id, params, video_script)
        if not video_terms:
            sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
            return

    save_script_data(task_id, video_script, video_terms, params)

    if stop_at == "terms":
        sm.state.update_task(
            task_id, state=const.TASK_STATE_COMPLETE, progress=100, terms=video_terms
        )
        return {"script": video_script, "terms": video_terms}

    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=20)

    # 3. Generate audio
    audio_file, audio_duration, sub_maker = generate_audio(
        task_id, params, video_script
    )
    if not audio_file:
        sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
        return

    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=30)

    if stop_at == "audio":
        sm.state.update_task(
            task_id,
            state=const.TASK_STATE_COMPLETE,
            progress=100,
            audio_file=audio_file,
        )
        return {"audio_file": audio_file, "audio_duration": audio_duration}

    # 4. Generate subtitle
    subtitle_path = generate_subtitle(
        task_id, params, video_script, sub_maker, audio_file
    )

    if stop_at == "subtitle":
        sm.state.update_task(
            task_id,
            state=const.TASK_STATE_COMPLETE,
            progress=100,
            subtitle_path=subtitle_path,
        )
        return {"subtitle_path": subtitle_path}

    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=40)

    # 5. Get video materials
    downloaded_videos = get_video_materials(
        task_id, params, video_terms, audio_duration
    )
    if not downloaded_videos:
        sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
        return

    if stop_at == "materials":
        sm.state.update_task(
            task_id,
            state=const.TASK_STATE_COMPLETE,
            progress=100,
            materials=downloaded_videos,
        )
        return {"materials": downloaded_videos}

    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=50)

    # 6. Generate final videos
    final_video_paths, combined_video_paths = generate_final_videos(
        task_id, params, downloaded_videos, audio_file, subtitle_path
    )

    if not final_video_paths:
        sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
        return

    logger.success(
        f"task {task_id} finished, generated {len(final_video_paths)} videos."
    )

    kwargs = {
        "videos": final_video_paths,
        "combined_videos": combined_video_paths,
        "script": video_script,
        "terms": video_terms,
        "audio_file": audio_file,
        "audio_duration": audio_duration,
        "subtitle_path": subtitle_path,
        "materials": downloaded_videos,
    }
    sm.state.update_task(
        task_id, state=const.TASK_STATE_COMPLETE, progress=100, **kwargs
    )
    return kwargs


def storyboard_start(task_id, params: VideoParams):
    """Storyboard phase: script → terms → materials only. Voiceover and
    subtitles are deferred to the segment-generation phase."""
    logger.info(f"start storyboard task: {task_id}")
    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=5)
    if type(params.video_concat_mode) is str:
        params.video_concat_mode = VideoConcatMode(params.video_concat_mode)

    video_script = generate_script(task_id, params)
    if not video_script or "Error: " in video_script:
        sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
        return None
    video_terms = ""
    if params.video_source != "local":
        video_terms = generate_terms(task_id, params, video_script)
        if not video_terms:
            sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
            return None
    save_script_data(task_id, video_script, video_terms, params)

    # No TTS yet — narration duration: use the user's target total duration if
    # set, otherwise estimate from script length (~4 chars/sec for CJK).
    target_total = getattr(params, "video_total_duration", 0) or 0
    if target_total > 0:
        est_duration = float(target_total)
    else:
        ascii_ratio = sum(1 for c in video_script if ord(c) < 128) / max(1, len(video_script))
        if ascii_ratio > 0.7:
            est_duration = max(10.0, len(video_script.split()) / 2.5)
        else:
            est_duration = max(10.0, len(video_script) / 4.0)

    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=30)
    if params.video_source == "llm":
        # Text-first storyboard: no images are generated here. Segment count is
        # sized from the estimated duration.
        segment_count = max(1, min(8, math.ceil(
            est_duration * params.video_count / max(1, params.video_clip_duration))))
        result = {"script": video_script, "terms": video_terms,
                  "materials": [], "segment_count": segment_count}
        # Drama mode: build the character cast + per-segment dialogue up front,
        # so the board can present characters and editable lines.
        if getattr(params, "presentation_mode", "narration") == "drama":
            drama = llm.generate_drama_storyboard(
                video_script, segment_count, target_duration=int(target_total))
            result["drama"] = drama
        sm.state.update_task(
            task_id, state=const.TASK_STATE_COMPLETE, progress=100, materials=[]
        )
        return result
    materials = get_video_materials(task_id, params, video_terms, est_duration)
    if not materials:
        sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
        return None
    sm.state.update_task(
        task_id, state=const.TASK_STATE_COMPLETE, progress=100, materials=materials
    )
    return {"script": video_script, "terms": video_terms, "materials": materials}


def _srt_ts(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def synthesize_drama_segment(task_dir, idx, dialogue_text, voice_map, subtitle_enabled):
    """Voice a character-performance segment line-by-line: each utterance uses
    its speaker's voice and an emotion-adjusted rate, then the clips are
    concatenated. Returns (audio_file, srt_file) or (None, "")."""
    from pydub import AudioSegment

    lines = voice.parse_dialogue_lines(dialogue_text)
    if not lines:
        return None, ""
    default_voice = next(iter(voice_map.values()), "zh-TW-HsiaoChenNeural")
    combined = AudioSegment.empty()
    srt_entries = []
    cursor = 0.0
    for li, ln in enumerate(lines):
        spk, emo, text = ln["speaker"], ln["emotion"], ln["line"]
        if not text:
            continue
        vname = voice_map.get(spk, default_voice)
        part_f = path.join(task_dir, f"seg-{idx}-line-{li}.mp3")
        # 依情感套用語速 + 音調（讓台詞有語調起伏）
        ok = voice.tts_emotion(text=text, voice_name=vname, emotion=emo, out_file=part_f)
        if not ok or not path.exists(part_f):
            logger.warning(f"segment {idx + 1} line {li + 1}: tts failed ({spk})")
            continue
        seg_audio = AudioSegment.from_file(part_f)
        dur = len(seg_audio) / 1000.0
        srt_entries.append((cursor, cursor + dur, (f"{spk}：" if spk else "") + text))
        combined += seg_audio
        combined += AudioSegment.silent(duration=250)  # small beat between lines
        cursor += dur + 0.25
        try:
            os.remove(part_f)
        except OSError:
            pass
    if len(combined) == 0:
        return None, ""
    audio_f = path.join(task_dir, f"seg-{idx}-audio.mp3")
    combined.export(audio_f, format="mp3")
    srt_f = ""
    if subtitle_enabled and srt_entries:
        srt_f = path.join(task_dir, f"seg-{idx}.srt")
        with open(srt_f, "w", encoding="utf-8") as f:
            for n, (st, et, txt) in enumerate(srt_entries, 1):
                f.write(f"{n}\n{_srt_ts(st)} --> {_srt_ts(et)}\n{txt}\n\n")
    return audio_f, srt_f


def _subtitle_from_dialogue(dialogue_text, duration, srt_path):
    """Build an SRT that spreads the dialogue lines evenly across `duration`
    (used when there is no TTS to time against — subtitle-only mode)."""
    lines = voice.parse_dialogue_lines(dialogue_text)
    lines = [ln for ln in lines if ln.get("line")]
    if not lines:
        return ""
    per = duration / len(lines)
    with open(srt_path, "w", encoding="utf-8") as f:
        for n, ln in enumerate(lines):
            st, et = n * per, (n + 1) * per
            txt = (f"{ln['speaker']}：" if ln.get("speaker") else "") + ln["line"]
            f.write(f"{n + 1}\n{_srt_ts(st)} --> {_srt_ts(et)}\n{txt}\n\n")
    return srt_path if os.path.exists(srt_path) else ""


def generate_segments(task_id, params: VideoParams, segments: list, voice_map: dict = None,
                      voice_mode: str = "tts"):
    """Render one reviewable video per storyboard segment. voice_mode:
    - 'tts': generate character voiceover (drama) / narration (narration mode).
    - 'subtitle_only': NO TTS — keep the clip's own audio (e.g. Veo-generated
      character speech) and only add a subtitle, avoiding double/overlapping
      voices. Returns segment paths ("" on failure)."""
    import copy

    task_dir = utils.task_dir(task_id)
    seg_params = copy.deepcopy(params)
    seg_params.bgm_type = ""  # bgm is mixed once at merge time, not per segment
    if type(seg_params.video_concat_mode) is str:
        seg_params.video_concat_mode = VideoConcatMode(seg_params.video_concat_mode)
    drama = getattr(params, "presentation_mode", "narration") == "drama"
    subtitle_only = voice_mode == "subtitle_only"

    outputs = []
    for i, seg in enumerate(segments):
        idx = int(seg.get("index", i))
        # 每段可設定演繹秒數（沒設就用全域設定）
        seg_dur = int(seg.get("duration") or params.video_clip_duration or 6)
        if drama:
            chunk = (seg.get("dialogue_text") or "").strip()
        else:
            chunk = (seg.get("script_chunk") or "").strip()
        clip = seg.get("clip") or ""
        image = seg.get("image") or ""
        if (not clip or not path.exists(clip)) and image and path.exists(image):
            m = MaterialInfo()
            m.provider = "llm"
            m.url = image
            pp = video.preprocess_video([m], clip_duration=seg_dur)
            if pp:
                clip = pp[0].url
        if not chunk or not clip or not path.exists(clip):
            logger.warning(f"segment {idx + 1}: missing script or clip, skipped")
            outputs.append("")
            continue
        logger.info(f"\n\n## rendering segment {idx + 1} ({'drama' if drama else 'narration'}, "
                    f"voice={voice_mode})")
        srt_f = ""
        if subtitle_only:
            # 只加字幕、不加配音：用畫面本身聲音（如 Veo 生成的角色語音），避免疊音
            audio_f = ""
            if params.subtitle_enabled:
                srt_f = path.join(task_dir, f"seg-{idx}.srt")
                srt_f = _subtitle_from_dialogue(chunk, seg_dur, srt_f)
        elif drama:
            audio_f, srt_f = synthesize_drama_segment(
                task_dir, idx, chunk, voice_map or {}, params.subtitle_enabled)
            if audio_f is None:
                logger.error(f"segment {idx + 1}: drama tts failed")
                outputs.append("")
                continue
        else:
            audio_f = path.join(task_dir, f"seg-{idx}-audio.mp3")
            sub_maker = voice.tts(
                text=chunk,
                voice_name=voice.parse_voice_name(params.voice_name),
                voice_rate=params.voice_rate,
                voice_file=audio_f,
            )
            if sub_maker is None:
                logger.error(f"segment {idx + 1}: tts failed")
                outputs.append("")
                continue
            if params.subtitle_enabled:
                srt_f = path.join(task_dir, f"seg-{idx}.srt")
                try:
                    voice.create_subtitle(sub_maker=sub_maker, text=chunk, subtitle_file=srt_f)
                except Exception as e:
                    logger.warning(f"segment {idx + 1}: subtitle failed: {e}")
                if not path.exists(srt_f):
                    srt_f = ""
        # 專用段落合成：畫面定格配合配音長度（不輪播）、保留 Veo 環境音、字幕烧录
        seg_out = path.join(task_dir, f"segment-{idx + 1}.mp4")
        try:
            video.compose_segment_video(
                clip_path=clip,
                audio_path=audio_f,
                subtitle_path=srt_f,
                output_file=seg_out,
                params=seg_params,
                min_duration=seg_dur,  # 段落至少達到設定秒數
                # 戲劇 TTS 模式：Veo 影片含角色對白人聲，去人聲只留環境音，
                # 避免與 TTS 配音疊音（subtitle_only 用 Veo 原音、不需去）
                strip_vocals=(drama and not subtitle_only),
            )
        except Exception as e:
            logger.error(f"segment {idx + 1}: compose failed: {e}")
        outputs.append(seg_out if path.exists(seg_out) else "")
    return outputs


def merge_segments(task_id, params: VideoParams, segment_files: list, transitions: list = None):
    """Merge confirmed segment videos into the final video. transitions (from
    each segment's connecting instruction) drive the effect between segments."""
    paired = [(f, (transitions or ["none"] * len(segment_files))[i] if transitions and i < len(transitions) else "none")
              for i, f in enumerate(segment_files) if f and path.exists(f)]
    if not paired:
        logger.error("merge_segments: no valid segment videos")
        sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
        return None
    segment_files = [p[0] for p in paired]
    seg_transitions = [p[1] for p in paired]
    final_path = path.join(utils.task_dir(task_id), "final-1.mp4")
    try:
        video.merge_segment_videos(segment_files, final_path, params, transitions=seg_transitions)
    except Exception as e:
        logger.error(f"merge_segments failed: {e}")
        sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
        return None
    kwargs = {"videos": [final_path], "combined_videos": segment_files}
    sm.state.update_task(task_id, state=const.TASK_STATE_COMPLETE, progress=100, **kwargs)
    return kwargs


def finalize(task_id, params: VideoParams, materials: list, video_script: str = ""):
    """Synthesize the final video for a storyboard task previously run with
    stop_at="materials". If video_script differs from the saved one (storyboard
    edits), the voiceover and subtitles are regenerated first."""
    task_dir = utils.task_dir(task_id)

    saved_script, script_data = "", {}
    script_file = path.join(task_dir, "script.json")
    try:
        with open(script_file, "r", encoding="utf-8") as f:
            script_data = json.loads(f.read())
        saved_script = script_data.get("script", "")
    except Exception:
        pass

    audio_file = path.join(task_dir, "audio.mp3")
    subtitle_path = path.join(task_dir, "subtitle.srt")
    script_changed = bool(video_script) and video_script.strip() != (saved_script or "").strip()

    if script_changed or not path.exists(audio_file):
        effective_script = video_script or saved_script
        if not effective_script:
            logger.error("finalize: no script available to (re)generate audio")
            sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
            return None
        logger.info("finalize: script edited — regenerating voiceover and subtitles")
        sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=30)
        audio_file, audio_duration, sub_maker = generate_audio(
            task_id, params, effective_script
        )
        if not audio_file:
            sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
            return None
        subtitle_path = generate_subtitle(
            task_id, params, effective_script, sub_maker, audio_file
        )
        if script_data:
            script_data["script"] = effective_script
            with open(script_file, "w", encoding="utf-8") as f:
                f.write(utils.to_json(script_data))
        saved_script = effective_script

    if not (params.subtitle_enabled and subtitle_path and path.exists(subtitle_path)):
        subtitle_path = ""
    video_script = saved_script

    if type(params.video_concat_mode) is str:
        params.video_concat_mode = VideoConcatMode(params.video_concat_mode)

    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=50)
    final_video_paths, combined_video_paths = generate_final_videos(
        task_id, params, materials, audio_file, subtitle_path
    )
    if not final_video_paths:
        sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
        return None

    logger.success(f"storyboard task {task_id} finalized, {len(final_video_paths)} videos.")
    kwargs = {
        "videos": final_video_paths,
        "combined_videos": combined_video_paths,
        "script": video_script,
        "materials": materials,
    }
    sm.state.update_task(task_id, state=const.TASK_STATE_COMPLETE, progress=100, **kwargs)
    return kwargs


if __name__ == "__main__":
    task_id = "task_id"
    params = VideoParams(
        video_subject="金钱的作用",
        voice_name="zh-CN-XiaoyiNeural-Female",
        voice_rate=1.0,
    )
    start(task_id, params, stop_at="video")
