import os
import random
from typing import List
from urllib.parse import urlencode

import requests
from loguru import logger
from moviepy.video.io.VideoFileClip import VideoFileClip

from app.config import config
from app.models.schema import MaterialInfo, VideoAspect, VideoConcatMode
from app.utils import utils

requested_count = 0


def get_api_key(cfg_key: str):
    api_keys = config.app.get(cfg_key)
    if not api_keys:
        raise ValueError(
            f"\n\n##### {cfg_key} is not set #####\n\nPlease set it in the config.toml file: {config.config_file}\n\n"
            f"{utils.to_json(config.app)}"
        )

    # if only one key is provided, return it
    if isinstance(api_keys, str):
        return api_keys

    global requested_count
    requested_count += 1
    return api_keys[requested_count % len(api_keys)]


def search_videos_pexels(
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect = VideoAspect.portrait,
) -> List[MaterialInfo]:
    aspect = VideoAspect(video_aspect)
    video_orientation = aspect.name
    video_width, video_height = aspect.to_resolution()
    api_key = get_api_key("pexels_api_keys")
    headers = {
        "Authorization": api_key,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36",
    }
    # Build URL
    params = {"query": search_term, "per_page": 20, "orientation": video_orientation}
    query_url = f"https://api.pexels.com/videos/search?{urlencode(params)}"
    logger.info(f"searching videos: {query_url}, with proxies: {config.proxy}")

    try:
        r = requests.get(
            query_url,
            headers=headers,
            proxies=config.proxy,
            verify=False,
            timeout=(30, 60),
        )
        response = r.json()
        video_items = []
        if "videos" not in response:
            logger.error(f"search videos failed: {response}")
            return video_items
        videos = response["videos"]
        # loop through each video in the result
        for v in videos:
            duration = v["duration"]
            # check if video has desired minimum duration
            if duration < minimum_duration:
                continue
            video_files = v["video_files"]
            # loop through each url to determine the best quality
            for video in video_files:
                w = int(video["width"])
                h = int(video["height"])
                if w == video_width and h == video_height:
                    item = MaterialInfo()
                    item.provider = "pexels"
                    item.url = video["link"]
                    item.duration = duration
                    video_items.append(item)
                    break
        return video_items
    except Exception as e:
        logger.error(f"search videos failed: {str(e)}")

    return []


def search_videos_pixabay(
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect = VideoAspect.portrait,
) -> List[MaterialInfo]:
    aspect = VideoAspect(video_aspect)

    video_width, video_height = aspect.to_resolution()

    api_key = get_api_key("pixabay_api_keys")
    # Build URL
    params = {
        "q": search_term,
        "video_type": "all",  # Accepted values: "all", "film", "animation"
        "per_page": 50,
        "key": api_key,
    }
    query_url = f"https://pixabay.com/api/videos/?{urlencode(params)}"
    logger.info(f"searching videos: {query_url}, with proxies: {config.proxy}")

    try:
        r = requests.get(
            query_url, proxies=config.proxy, verify=False, timeout=(30, 60)
        )
        response = r.json()
        video_items = []
        if "hits" not in response:
            logger.error(f"search videos failed: {response}")
            return video_items
        videos = response["hits"]
        # loop through each video in the result
        for v in videos:
            duration = v["duration"]
            # check if video has desired minimum duration
            if duration < minimum_duration:
                continue
            video_files = v["videos"]
            # loop through each url to determine the best quality
            for video_type in video_files:
                video = video_files[video_type]
                w = int(video["width"])
                # h = int(video["height"])
                if w >= video_width:
                    item = MaterialInfo()
                    item.provider = "pixabay"
                    item.url = video["url"]
                    item.duration = duration
                    video_items.append(item)
                    break
        return video_items
    except Exception as e:
        logger.error(f"search videos failed: {str(e)}")

    return []


def save_video(video_url: str, save_dir: str = "") -> str:
    if not save_dir:
        save_dir = utils.storage_dir("cache_videos")

    if not os.path.exists(save_dir):
        os.makedirs(save_dir)

    url_without_query = video_url.split("?")[0]
    url_hash = utils.md5(url_without_query)
    video_id = f"vid-{url_hash}"
    video_path = f"{save_dir}/{video_id}.mp4"

    # if video already exists, return the path
    if os.path.exists(video_path) and os.path.getsize(video_path) > 0:
        logger.info(f"video already exists: {video_path}")
        return video_path

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
    }

    # if video does not exist, download it
    with open(video_path, "wb") as f:
        f.write(
            requests.get(
                video_url,
                headers=headers,
                proxies=config.proxy,
                verify=False,
                timeout=(60, 240),
            ).content
        )

    if os.path.exists(video_path) and os.path.getsize(video_path) > 0:
        try:
            clip = VideoFileClip(video_path)
            duration = clip.duration
            fps = clip.fps
            clip.close()
            if duration > 0 and fps > 0:
                return video_path
        except Exception as e:
            try:
                os.remove(video_path)
            except Exception:
                pass
            logger.warning(f"invalid video file: {video_path} => {str(e)}")
    return ""


def download_videos(
    task_id: str,
    search_terms: List[str],
    source: str = "pexels",
    video_aspect: VideoAspect = VideoAspect.portrait,
    video_contact_mode: VideoConcatMode = VideoConcatMode.random,
    audio_duration: float = 0.0,
    max_clip_duration: int = 5,
) -> List[str]:
    valid_video_items = []
    valid_video_urls = []
    found_duration = 0.0
    search_videos = search_videos_pexels
    if source == "pixabay":
        search_videos = search_videos_pixabay

    for search_term in search_terms:
        video_items = search_videos(
            search_term=search_term,
            minimum_duration=max_clip_duration,
            video_aspect=video_aspect,
        )
        logger.info(f"found {len(video_items)} videos for '{search_term}'")

        for item in video_items:
            if item.url not in valid_video_urls:
                valid_video_items.append(item)
                valid_video_urls.append(item.url)
                found_duration += item.duration

    logger.info(
        f"found total videos: {len(valid_video_items)}, required duration: {audio_duration} seconds, found duration: {found_duration} seconds"
    )
    video_paths = []

    material_directory = config.app.get("material_directory", "").strip()
    if material_directory == "task":
        material_directory = utils.task_dir(task_id)
    elif material_directory and not os.path.isdir(material_directory):
        material_directory = ""

    if video_contact_mode.value == VideoConcatMode.random.value:
        random.shuffle(valid_video_items)

    total_duration = 0.0
    for item in valid_video_items:
        try:
            logger.info(f"downloading video: {item.url}")
            saved_video_path = save_video(
                video_url=item.url, save_dir=material_directory
            )
            if saved_video_path:
                logger.info(f"video saved: {saved_video_path}")
                video_paths.append(saved_video_path)
                seconds = min(max_clip_duration, item.duration)
                total_duration += seconds
                if total_duration > audio_duration:
                    logger.info(
                        f"total duration of downloaded videos: {total_duration} seconds, skip downloading more"
                    )
                    break
        except Exception as e:
            logger.error(f"failed to download video: {utils.to_json(item)} => {str(e)}")
    logger.success(f"downloaded {len(video_paths)} videos")
    return video_paths


def generate_images_llm(
    task_id: str,
    search_terms: List[str],
    video_aspect: VideoAspect = VideoAspect.portrait,
    audio_duration: float = 0.0,
    max_clip_duration: int = 5,
) -> List[MaterialInfo]:
    """Generate image materials via the LLM proxy image model.

    Sends chat completions with modalities=["image"]; the proxy decides the
    actual image backend from its own config (image_model), so the model is
    adjustable from the proxy admin backend without touching this app.
    """
    import base64
    import math

    base_url = config.app.get("openai_base_url", "").rstrip("/")
    api_key = config.app.get("openai_api_key", "")
    if not base_url or not api_key:
        logger.error("openai_base_url / openai_api_key not set, cannot generate images")
        return []

    aspect = VideoAspect(video_aspect)
    ratio = {"landscape": "16:9", "portrait": "9:16", "square": "1:1"}.get(
        aspect.name, "9:16"
    )

    count = max(1, math.ceil(audio_duration / max_clip_duration)) if audio_duration else 3
    count = min(count, 8)  # cap generation cost per task

    save_dir = utils.task_dir(task_id)
    materials = []
    for i in range(count):
        term = search_terms[i % len(search_terms)] if search_terms else "cinematic scene"
        prompt = (
            f"A high quality, photorealistic, cinematic stock photo of: {term}. "
            "No text, no watermark, no captions."
        )
        try:
            resp = requests.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    # "video-image" tags this request as the video generator's,
                    # so the proxy uses its separate video_image_model setting.
                    "model": "video-image",
                    "modalities": ["image"],
                    "messages": [
                        {"role": "system", "content": f"aspect_ratio={ratio}"},
                        {"role": "user", "content": prompt},
                    ],
                },
                timeout=300,
            )
            resp.raise_for_status()
            data = resp.json()
            mod = data["choices"][0]["message"].get("multi_mod_content", [])
            b64 = next((p.get("data") for p in mod if p.get("type") == "image"), None)
            if not b64:
                logger.warning(f"no image returned for term: {term}")
                continue
            image_path = os.path.join(save_dir, f"llm-image-{i}.png")
            with open(image_path, "wb") as f:
                f.write(base64.b64decode(b64))
            logger.success(f"image generated: {image_path}")
            item = MaterialInfo()
            item.provider = "llm"
            item.url = image_path
            item.duration = max_clip_duration
            materials.append(item)
        except Exception as e:
            logger.error(f"failed to generate image for '{term}': {str(e)}")
    return materials


def generate_single_image_llm(
    task_id: str,
    prompt: str,
    video_aspect: VideoAspect = VideoAspect.portrait,
    index: int = 0,
    style: str = "",
    reference_images: list = None,
    appearance: str = "",
    out_name: str = "",
) -> str:
    """(Re)generate one storyboard image. When reference_images (paths to
    character reference portraits) are given, they are passed to the image model
    so recurring characters keep a CONSISTENT appearance across segments;
    appearance is an optional locked physical description appended to the prompt.
    Writes llm-image-{index}.png (or out_name) in the task dir; returns path or ""."""
    import base64

    base_url = config.app.get("openai_base_url", "").rstrip("/")
    api_key = config.app.get("openai_api_key", "")
    if not base_url or not api_key:
        logger.error("openai_base_url / openai_api_key not set")
        return ""
    aspect = VideoAspect(video_aspect)
    ratio = {"landscape": "16:9", "portrait": "9:16", "square": "1:1"}.get(aspect.name, "9:16")
    style_part = f" Visual style: {style.strip()}." if (style or "").strip() else ""
    appearance_part = f" Keep these characters' appearance EXACTLY consistent: {appearance.strip()}." \
        if (appearance or "").strip() else ""
    ref_note = " Use the provided reference image(s) as the exact appearance of the character(s)." \
        if reference_images else ""
    full_prompt = (
        f"A high quality, photorealistic, cinematic stock photo of: {prompt}.{style_part}"
        f"{appearance_part}{ref_note} No text, no watermark, no captions."
    )

    # Build user content — attach reference images as image_url parts so the
    # proxy routes to images/edits with them.
    user_content = full_prompt
    valid_refs = [p for p in (reference_images or []) if p and os.path.exists(p)]
    if valid_refs:
        parts = [{"type": "text", "text": full_prompt}]
        for rp in valid_refs[:4]:  # cap to keep the edit request bounded
            try:
                with open(rp, "rb") as f:
                    b64ref = base64.b64encode(f.read()).decode()
                mime = "image/png" if rp.lower().endswith(".png") else "image/jpeg"
                parts.append({"type": "image_url",
                              "image_url": {"url": f"data:{mime};base64,{b64ref}"}})
            except Exception as e:
                logger.warning(f"skip reference image {rp}: {e}")
        user_content = parts

    try:
        resp = requests.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": "video-image",
                "modalities": ["image"],
                "messages": [
                    {"role": "system", "content": f"aspect_ratio={ratio}"},
                    {"role": "user", "content": user_content},
                ],
            },
            timeout=300,
        )
        resp.raise_for_status()
        mod = resp.json()["choices"][0]["message"].get("multi_mod_content", [])
        b64 = next((p.get("data") for p in mod if p.get("type") == "image"), None)
        if not b64:
            return ""
        fname = out_name or f"llm-image-{index}.png"
        image_path = os.path.join(utils.task_dir(task_id), fname)
        with open(image_path, "wb") as f:
            f.write(base64.b64decode(b64))
        logger.success(f"storyboard image generated: {image_path} (refs={len(valid_refs)})")
        return image_path
    except Exception as e:
        logger.error(f"failed to generate image: {str(e)}")
        return ""


def generate_character_reference(task_id: str, char_name: str, appearance: str,
                                 video_aspect: VideoAspect = VideoAspect.portrait,
                                 style: str = "") -> str:
    """Generate a one-time reference portrait for a character (its 'model sheet'),
    used to keep the character's appearance consistent across all segment images.
    Writes char-ref-{safe_name}.png; returns path or ""."""
    import re as _re
    safe = _re.sub(r"[^\w\-]", "_", char_name)[:24] or "char"
    prompt = (f"Full-body character reference portrait of {char_name}: {appearance}. "
              "Single character, neutral background, clear front view, consistent design")
    return generate_single_image_llm(
        task_id, prompt, video_aspect, style=style,
        out_name=f"char-ref-{safe}.png")


def generate_single_video_llm(
    task_id: str,
    prompt: str,
    video_aspect: VideoAspect = VideoAspect.portrait,
    max_clip_duration: int = 5,
    index: int = 0,
    style: str = "",
    reference_image: str = "",
    note_out: dict = None,
    appearance: str = "",
) -> str:
    """Generate ONE storyboard segment video with Veo. When reference_image (a
    path to that segment's storyboard image) is given, image-to-video is used:
    the storyboard image becomes the first frame / visual anchor and the
    (user-editable) video direction script drives the motion. Otherwise plain
    text-to-video is used. Duration snaps DOWN to ≤ max_clip_duration (4/6/8s).
    Overwrites llm-video-{index}.mp4 in the task dir; returns the path or ""."""
    import base64

    base_url = config.app.get("openai_base_url", "").rstrip("/")
    api_key = config.app.get("openai_api_key", "")
    if not base_url or not api_key:
        logger.error("openai_base_url / openai_api_key not set")
        return ""
    aspect = VideoAspect(video_aspect)
    ratio = {"landscape": "16:9", "portrait": "9:16", "square": "16:9"}.get(aspect.name, "9:16")
    duration = max((d for d in (4, 6, 8) if d <= max_clip_duration), default=4)
    style_part = f" Visual style: {style.strip()}." if (style or "").strip() else ""
    # 人物外型設定注入：讓 Veo 生成的角色（尤其純文字 text-to-video fallback、
    # 無參考圖時）嚴格符合上方角色設定，維持長相一致。
    appear_part = (f" 畫面中人物外型務必嚴格符合以下設定並保持一致：{appearance.strip()}."
                   if (appearance or "").strip() else "")
    full_prompt = (
        f"{prompt.strip()}.{style_part}{appear_part} Photorealistic, cinematic quality, smooth camera motion. "
        "No text, no watermark, no captions, no subtitles."
    )
    payload = {
        "prompt": full_prompt,
        "duration_seconds": duration,
        "aspect_ratio": ratio,
    }
    # Image-to-video: use the segment's storyboard image as the first frame.
    if reference_image and os.path.exists(reference_image):
        try:
            with open(reference_image, "rb") as f:
                payload["image_b64"] = base64.b64encode(f.read()).decode()
            payload["image_mime"] = "image/png" if reference_image.lower().endswith(".png") else "image/jpeg"
            logger.info(f"segment video uses reference image: {os.path.basename(reference_image)}")
        except Exception as e:
            logger.warning(f"failed to read reference image, falling back to text-to-video: {e}")
    import time as _time
    video_path = os.path.join(utils.task_dir(task_id), f"llm-video-{index}.mp4")

    def _post_veo(pl):
        """送一次 Veo 請求；瞬態失敗（502/503/504/超時）退避重試最多 3 次。
        成功回傳 video_path，全部失敗回傳 ("", 錯誤字串)。"""
        _last = ""
        for _attempt in range(3):
            try:
                resp = requests.post(
                    f"{base_url}/videos/generations",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json=pl, timeout=660,
                )
                resp.raise_for_status()
                b64 = resp.json()["data"][0]["b64_json"]
                with open(video_path, "wb") as f:
                    f.write(base64.b64decode(b64))
                logger.success(f"storyboard segment video generated: {video_path} ({duration}s)")
                return video_path, ""
            except Exception as e:
                _last = str(e)
                _body = ""
                try:
                    _body = e.response.text if getattr(e, "response", None) is not None else ""
                except Exception:
                    pass
                # Veo 內容審查拒絕（圖片/提示違反使用規範）不是瞬態，重試無用
                if "usage guidelines" in _body or "violates" in _body or "Support codes" in _body:
                    logger.warning(f"segment video rejected by Veo content policy: {_body[:200]}")
                    return "", "content_policy"
                _status = getattr(getattr(e, "response", None), "status_code", None)
                _transient = _status in (429, 500, 502, 503, 504) or isinstance(
                    e, (requests.exceptions.Timeout, requests.exceptions.ConnectionError))
                if _transient and _attempt < 2:
                    _wait = 5 * (_attempt + 1)
                    logger.warning(f"segment video attempt {_attempt + 1} failed "
                                   f"({_status or type(e).__name__}), retry in {_wait}s")
                    _time.sleep(_wait)
                    continue
                break
        return "", _last

    vid, err = _post_veo(payload)
    if vid:
        if note_out is not None:
            note_out["motion"] = ""
        return vid
    _rejected = (err == "content_policy")
    # image-to-video 因參考圖被 Veo 內容審查拒絕（或其他失敗）→ 改用純文字生成，
    # 讓該段仍是動態演繹而非靜態圖 zoom。
    if payload.get("image_b64"):
        logger.warning("image-to-video failed，改用純文字 text-to-video 重試該段")
        payload.pop("image_b64", None)
        payload.pop("image_mime", None)
        vid, err = _post_veo(payload)
        if vid:
            if note_out is not None:
                note_out["motion"] = "text_fallback_policy" if _rejected else "text_fallback"
            return vid
    if note_out is not None:
        note_out["motion"] = "failed"
    logger.error(f"failed to generate segment video after retries: {err}")
    return ""


def generate_videos_llm(
    task_id: str,
    search_terms: List[str],
    video_aspect: VideoAspect = VideoAspect.portrait,
    audio_duration: float = 0.0,
    max_clip_duration: int = 5,
) -> List[MaterialInfo]:
    """Generate video clips with Veo via the LLM proxy /v1/videos/generations.

    The actual Veo model is chosen by the proxy config (video_model), so it is
    adjustable from the proxy admin backend. Note: Veo is billed per second of
    generated video — clip count is capped to keep cost bounded.
    """
    import base64
    import math
    from concurrent.futures import ThreadPoolExecutor

    base_url = config.app.get("openai_base_url", "").rstrip("/")
    api_key = config.app.get("openai_api_key", "")
    if not base_url or not api_key:
        logger.error("openai_base_url / openai_api_key not set, cannot generate videos")
        return []

    aspect = VideoAspect(video_aspect)
    ratio = {"landscape": "16:9", "portrait": "9:16", "square": "16:9"}.get(
        aspect.name, "9:16"
    )
    # Veo supports 4/6/8 second clips; snap the configured clip duration
    duration = min((4, 6, 8), key=lambda d: abs(d - max_clip_duration))

    count = max(1, math.ceil(audio_duration / duration)) if audio_duration else 2
    count = min(count, 5)  # hard cap: Veo is billed per second, keep cost bounded

    save_dir = utils.task_dir(task_id)
    concurrency = int(config.app.get("video_gen_concurrency", 2))

    def _gen_one(i: int):
        term = search_terms[i % len(search_terms)] if search_terms else "cinematic scene"
        prompt = (
            f"A high quality, photorealistic, cinematic video of: {term}. "
            "Smooth camera motion. No text, no watermark, no captions."
        )
        try:
            resp = requests.post(
                f"{base_url}/videos/generations",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "prompt": prompt,
                    "duration_seconds": duration,
                    "aspect_ratio": ratio,
                },
                timeout=660,
            )
            resp.raise_for_status()
            data = resp.json()
            b64 = data["data"][0]["b64_json"]
            video_path = os.path.join(save_dir, f"llm-video-{i}.mp4")
            with open(video_path, "wb") as f:
                f.write(base64.b64decode(b64))
            logger.success(f"veo video generated: {video_path}")
            item = MaterialInfo()
            item.provider = "veo"
            item.url = video_path
            item.duration = duration
            return item
        except Exception as e:
            logger.error(f"failed to generate video for '{term}': {str(e)}")
            return None

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        results = list(pool.map(_gen_one, range(count)))
    return [m for m in results if m]


if __name__ == "__main__":
    download_videos(
        "test123", ["Money Exchange Medium"], audio_duration=100, source="pixabay"
    )
