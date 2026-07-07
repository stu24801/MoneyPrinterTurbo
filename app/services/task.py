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
    vid = material.generate_single_video_llm(
        task_id, prompt, aspect, max_clip_duration=max_dur, index=uid,
        style=style, reference_image=reference_image or "")
    if not vid:
        raise RuntimeError("video generation returned empty")
    data = _load_sb(task_id)
    for s in data.get("segments", []):
        if s.get("uid") == uid:
            s["clip"] = vid
            s["video_prompt"] = prompt
            break
    _save_sb(task_id, data)
    return {"clip": vid, "uid": uid}


def job_render_segments(task_id, params: VideoParams, voice_map, seg_inputs,
                        auto_motion=False):
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
    for pos, inp in enumerate(seg_inputs):
        idx = int(inp.get("index", pos))
        data = _load_sb(task_id)
        segs = data.get("segments", [])
        # 自愈：段落沒有分鏡圖也沒有影片 → 先自動產生分鏡圖（否則無法渲染）
        if 0 <= idx < len(segs):
            s = segs[idx]
            _clip = s.get("clip") or ""
            _image = s.get("image") or ""
            _has_clip = _clip and os.path.exists(_clip)
            _has_image = _image and os.path.exists(_image)
            if not _has_clip and not _has_image:
                jobs.update_progress(task_id, "batch", pos, total, f"segment {idx + 1} · image")
                _iprompt = (s.get("prompt") or s.get("video_prompt")
                            or s.get("scene") or s.get("dialogue_text")
                            or s.get("script_chunk") or "cinematic scene")
                # 戲劇模式：帶入該段角色外型，維持一致長相
                _appear = ""
                if characters:
                    _spk = {ln["speaker"] for ln in voice.parse_dialogue_lines(
                        s.get("dialogue_text", "") or "") if ln.get("speaker")}
                    _present = [c for c in characters if c.get("name") in _spk] or characters
                    _appear = "；".join(f"{c.get('name','')}：{c.get('appearance','')}"
                                        for c in _present if c.get("appearance"))
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
                vdir = s.get("video_prompt") or s.get("dialogue_text") or s.get("script_chunk") or ""
                _seg_dur = int(s.get("duration") or params.video_clip_duration or 6)
                vid = material.generate_single_video_llm(
                    task_id, vdir, VideoAspect(_aspect_value(params)),
                    max_clip_duration=_seg_dur, index=s.get("uid", idx),
                    style=style, reference_image=image)
                if vid:
                    data = _load_sb(task_id)
                    segs = data.get("segments", [])
                    segs[idx]["clip"] = vid
                    inp["clip"] = vid
                    _save_sb(task_id, data)
        outs = generate_segments(task_id, params, [inp], voice_map=voice_map)
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


def job_merge(task_id, params: VideoParams, use_transitions=True):
    """Merge all rendered segment videos into the final film. When
    use_transitions is False, segments are joined with hard cuts (no fade/slide)
    so the performance flows continuously without being interrupted."""
    data = _load_sb(task_id)
    segs = data.get("segments", [])
    files = [s.get("segment_video") for s in segs]
    if use_transitions:
        fx = [s.get("transition_effect", "none") for s in segs]
    else:
        fx = ["none"] * len(segs)
    fin = merge_segments(task_id, params, files, transitions=fx)
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
        rate = voice.emotion_to_rate(emo)
        part_f = path.join(task_dir, f"seg-{idx}-line-{li}.mp3")
        sm = voice.tts(text=text, voice_name=vname, voice_rate=rate, voice_file=part_f)
        if sm is None or not path.exists(part_f):
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


def generate_segments(task_id, params: VideoParams, segments: list, voice_map: dict = None):
    """Render one reviewable video per storyboard segment. In narration mode a
    segment is voiced from script_chunk; in drama mode from dialogue_text, using
    per-character voices and emotion. Returns segment paths ("" on failure)."""
    import copy

    task_dir = utils.task_dir(task_id)
    seg_params = copy.deepcopy(params)
    seg_params.bgm_type = ""  # bgm is mixed once at merge time, not per segment
    if type(seg_params.video_concat_mode) is str:
        seg_params.video_concat_mode = VideoConcatMode(seg_params.video_concat_mode)
    drama = getattr(params, "presentation_mode", "narration") == "drama"

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
        logger.info(f"\n\n## rendering segment {idx + 1} ({'drama' if drama else 'narration'})")
        srt_f = ""
        if drama:
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
        combined_f = path.join(task_dir, f"seg-{idx}-combined.mp4")
        video.combine_videos(
            combined_video_path=combined_f,
            video_paths=[clip],
            audio_file=audio_f,
            video_aspect=params.video_aspect,
            video_concat_mode=VideoConcatMode.sequential,
            video_transition_mode=params.video_transition_mode,
            max_clip_duration=seg_dur,
            threads=params.n_threads,
        )
        seg_out = path.join(task_dir, f"segment-{idx + 1}.mp4")
        video.generate_video(
            video_path=combined_f,
            audio_path=audio_f,
            subtitle_path=srt_f,
            output_file=seg_out,
            params=seg_params,
        )
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
