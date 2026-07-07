import glob
import itertools
import os
import random
import gc
import shutil
from typing import List
from loguru import logger
from moviepy import (
    AudioFileClip,
    ColorClip,
    CompositeAudioClip,
    CompositeVideoClip,
    ImageClip,
    TextClip,
    VideoFileClip,
    afx,
    concatenate_videoclips,
)
from moviepy.video.tools.subtitles import SubtitlesClip
from PIL import ImageFont

from app.models import const
from app.models.schema import (
    MaterialInfo,
    VideoAspect,
    VideoConcatMode,
    VideoParams,
    VideoTransitionMode,
)
from app.services.utils import video_effects
from app.utils import utils

class SubClippedVideoClip:
    def __init__(self, file_path, start_time=None, end_time=None, width=None, height=None, duration=None):
        self.file_path = file_path
        self.start_time = start_time
        self.end_time = end_time
        self.width = width
        self.height = height
        if duration is None:
            self.duration = end_time - start_time
        else:
            self.duration = duration

    def __str__(self):
        return f"SubClippedVideoClip(file_path={self.file_path}, start_time={self.start_time}, end_time={self.end_time}, duration={self.duration}, width={self.width}, height={self.height})"


audio_codec = "aac"
video_codec = "libx264"
fps = 30

def close_clip(clip):
    if clip is None:
        return
        
    try:
        # close main resources
        if hasattr(clip, 'reader') and clip.reader is not None:
            clip.reader.close()
            
        # close audio resources
        if hasattr(clip, 'audio') and clip.audio is not None:
            if hasattr(clip.audio, 'reader') and clip.audio.reader is not None:
                clip.audio.reader.close()
            del clip.audio
            
        # close mask resources
        if hasattr(clip, 'mask') and clip.mask is not None:
            if hasattr(clip.mask, 'reader') and clip.mask.reader is not None:
                clip.mask.reader.close()
            del clip.mask
            
        # handle child clips in composite clips
        if hasattr(clip, 'clips') and clip.clips:
            for child_clip in clip.clips:
                if child_clip is not clip:  # avoid possible circular references
                    close_clip(child_clip)
            
        # clear clip list
        if hasattr(clip, 'clips'):
            clip.clips = []
            
    except Exception as e:
        logger.error(f"failed to close clip: {str(e)}")
    
    del clip
    gc.collect()

def delete_files(files: List[str] | str):
    if isinstance(files, str):
        files = [files]
        
    for file in files:
        try:
            os.remove(file)
        except:
            pass

def get_bgm_file(bgm_type: str = "random", bgm_file: str = ""):
    if not bgm_type:
        return ""

    if bgm_file and os.path.exists(bgm_file):
        return bgm_file

    if bgm_type == "random":
        suffix = "*.mp3"
        song_dir = utils.song_dir()
        files = glob.glob(os.path.join(song_dir, suffix))
        return random.choice(files)

    return ""


def combine_videos(
    combined_video_path: str,
    video_paths: List[str],
    audio_file: str,
    video_aspect: VideoAspect = VideoAspect.portrait,
    video_concat_mode: VideoConcatMode = VideoConcatMode.random,
    video_transition_mode: VideoTransitionMode = None,
    max_clip_duration: int = 5,
    threads: int = 2,
) -> str:
    audio_clip = AudioFileClip(audio_file)
    audio_duration = audio_clip.duration
    logger.info(f"audio duration: {audio_duration} seconds")
    # Required duration of each clip
    req_dur = audio_duration / len(video_paths)
    req_dur = max_clip_duration
    logger.info(f"maximum clip duration: {req_dur} seconds")
    output_dir = os.path.dirname(combined_video_path)

    aspect = VideoAspect(video_aspect)
    video_width, video_height = aspect.to_resolution()

    processed_clips = []
    subclipped_items = []
    video_duration = 0
    for video_path in video_paths:
        clip = VideoFileClip(video_path)
        clip_duration = clip.duration
        clip_w, clip_h = clip.size
        close_clip(clip)
        
        start_time = 0

        while start_time < clip_duration:
            end_time = min(start_time + max_clip_duration, clip_duration)            
            if clip_duration - start_time >= max_clip_duration:
                subclipped_items.append(SubClippedVideoClip(file_path= video_path, start_time=start_time, end_time=end_time, width=clip_w, height=clip_h))
            start_time = end_time    
            if video_concat_mode.value == VideoConcatMode.sequential.value:
                break

    # random subclipped_items order
    if video_concat_mode.value == VideoConcatMode.random.value:
        random.shuffle(subclipped_items)
        
    logger.debug(f"total subclipped items: {len(subclipped_items)}")
    
    # Add downloaded clips over and over until the duration of the audio (max_duration) has been reached
    for i, subclipped_item in enumerate(subclipped_items):
        if video_duration > audio_duration:
            break
        
        logger.debug(f"processing clip {i+1}: {subclipped_item.width}x{subclipped_item.height}, current duration: {video_duration:.2f}s, remaining: {audio_duration - video_duration:.2f}s")
        
        try:
            clip = VideoFileClip(subclipped_item.file_path).subclipped(subclipped_item.start_time, subclipped_item.end_time)
            clip_duration = clip.duration
            # Not all videos are same size, so we need to resize them
            clip_w, clip_h = clip.size
            if clip_w != video_width or clip_h != video_height:
                clip_ratio = clip.w / clip.h
                video_ratio = video_width / video_height
                logger.debug(f"resizing clip, source: {clip_w}x{clip_h}, ratio: {clip_ratio:.2f}, target: {video_width}x{video_height}, ratio: {video_ratio:.2f}")
                
                if clip_ratio == video_ratio:
                    clip = clip.resized(new_size=(video_width, video_height))
                else:
                    if clip_ratio > video_ratio:
                        scale_factor = video_width / clip_w
                    else:
                        scale_factor = video_height / clip_h

                    new_width = int(clip_w * scale_factor)
                    new_height = int(clip_h * scale_factor)

                    background = ColorClip(size=(video_width, video_height), color=(0, 0, 0)).with_duration(clip_duration)
                    clip_resized = clip.resized(new_size=(new_width, new_height)).with_position("center")
                    clip = CompositeVideoClip([background, clip_resized])
                    
            shuffle_side = random.choice(["left", "right", "top", "bottom"])
            if (
                video_transition_mode is None
                or video_transition_mode.value == VideoTransitionMode.none.value
            ):
                clip = clip
            elif video_transition_mode.value == VideoTransitionMode.fade_in.value:
                clip = video_effects.fadein_transition(clip, 1)
            elif video_transition_mode.value == VideoTransitionMode.fade_out.value:
                clip = video_effects.fadeout_transition(clip, 1)
            elif video_transition_mode.value == VideoTransitionMode.slide_in.value:
                clip = video_effects.slidein_transition(clip, 1, shuffle_side)
            elif video_transition_mode.value == VideoTransitionMode.slide_out.value:
                clip = video_effects.slideout_transition(clip, 1, shuffle_side)
            elif video_transition_mode.value == VideoTransitionMode.shuffle.value:
                transition_funcs = [
                    lambda c: video_effects.fadein_transition(c, 1),
                    lambda c: video_effects.fadeout_transition(c, 1),
                    lambda c: video_effects.slidein_transition(c, 1, shuffle_side),
                    lambda c: video_effects.slideout_transition(c, 1, shuffle_side),
                ]
                shuffle_transition = random.choice(transition_funcs)
                clip = shuffle_transition(clip)

            if clip.duration > max_clip_duration:
                clip = clip.subclipped(0, max_clip_duration)
                
            # wirte clip to temp file
            clip_file = f"{output_dir}/temp-clip-{i+1}.mp4"
            clip.write_videofile(clip_file, logger=None, fps=fps, codec=video_codec)
            
            close_clip(clip)
        
            processed_clips.append(SubClippedVideoClip(file_path=clip_file, duration=clip.duration, width=clip_w, height=clip_h))
            video_duration += clip.duration
            
        except Exception as e:
            logger.error(f"failed to process clip: {str(e)}")
    
    # loop processed clips until the video duration matches or exceeds the audio duration.
    if video_duration < audio_duration:
        logger.warning(f"video duration ({video_duration:.2f}s) is shorter than audio duration ({audio_duration:.2f}s), looping clips to match audio length.")
        base_clips = processed_clips.copy()
        for clip in itertools.cycle(base_clips):
            if video_duration >= audio_duration:
                break
            processed_clips.append(clip)
            video_duration += clip.duration
        logger.info(f"video duration: {video_duration:.2f}s, audio duration: {audio_duration:.2f}s, looped {len(processed_clips)-len(base_clips)} clips")
     
    # merge video clips progressively, avoid loading all videos at once to avoid memory overflow
    logger.info("starting clip merging process")
    if not processed_clips:
        logger.warning("no clips available for merging")
        return combined_video_path
    
    # if there is only one clip, use it directly
    if len(processed_clips) == 1:
        logger.info("using single clip directly")
        shutil.copy(processed_clips[0].file_path, combined_video_path)
        delete_files(processed_clips)
        logger.info("video combining completed")
        return combined_video_path
    
    # create initial video file as base
    base_clip_path = processed_clips[0].file_path
    temp_merged_video = f"{output_dir}/temp-merged-video.mp4"
    temp_merged_next = f"{output_dir}/temp-merged-next.mp4"
    
    # copy first clip as initial merged video
    shutil.copy(base_clip_path, temp_merged_video)
    
    # merge remaining video clips one by one
    for i, clip in enumerate(processed_clips[1:], 1):
        logger.info(f"merging clip {i}/{len(processed_clips)-1}, duration: {clip.duration:.2f}s")
        
        try:
            # load current base video and next clip to merge
            base_clip = VideoFileClip(temp_merged_video)
            next_clip = VideoFileClip(clip.file_path)
            
            # merge these two clips
            merged_clip = concatenate_videoclips([base_clip, next_clip])

            # save merged result to temp file
            merged_clip.write_videofile(
                filename=temp_merged_next,
                threads=threads,
                logger=None,
                temp_audiofile_path=output_dir,
                audio_codec=audio_codec,
                fps=fps,
            )
            close_clip(base_clip)
            close_clip(next_clip)
            close_clip(merged_clip)
            
            # replace base file with new merged file
            delete_files(temp_merged_video)
            os.rename(temp_merged_next, temp_merged_video)
            
        except Exception as e:
            logger.error(f"failed to merge clip: {str(e)}")
            continue
    
    # after merging, rename final result to target file name
    os.rename(temp_merged_video, combined_video_path)
    
    # clean temp files
    clip_files = [clip.file_path for clip in processed_clips]
    delete_files(clip_files)
            
    logger.info("video combining completed")
    return combined_video_path


def wrap_text(text, max_width, font="Arial", fontsize=60):
    # Create ImageFont
    font = ImageFont.truetype(font, fontsize)

    def get_text_size(inner_text):
        inner_text = inner_text.strip()
        left, top, right, bottom = font.getbbox(inner_text)
        return right - left, bottom - top

    width, height = get_text_size(text)
    if width <= max_width:
        return text, height

    processed = True

    _wrapped_lines_ = []
    words = text.split(" ")
    _txt_ = ""
    for word in words:
        _before = _txt_
        _txt_ += f"{word} "
        _width, _height = get_text_size(_txt_)
        if _width <= max_width:
            continue
        else:
            if _txt_.strip() == word.strip():
                processed = False
                break
            _wrapped_lines_.append(_before)
            _txt_ = f"{word} "
    _wrapped_lines_.append(_txt_)
    if processed:
        _wrapped_lines_ = [line.strip() for line in _wrapped_lines_]
        result = "\n".join(_wrapped_lines_).strip()
        height = len(_wrapped_lines_) * height
        return result, height

    _wrapped_lines_ = []
    chars = list(text)
    _txt_ = ""
    for word in chars:
        _txt_ += word
        _width, _height = get_text_size(_txt_)
        if _width <= max_width:
            continue
        else:
            _wrapped_lines_.append(_txt_)
            _txt_ = ""
    _wrapped_lines_.append(_txt_)
    result = "\n".join(_wrapped_lines_).strip()
    height = len(_wrapped_lines_) * height
    return result, height


def generate_video(
    video_path: str,
    audio_path: str,
    subtitle_path: str,
    output_file: str,
    params: VideoParams,
):
    aspect = VideoAspect(params.video_aspect)
    video_width, video_height = aspect.to_resolution()

    logger.info(f"generating video: {video_width} x {video_height}")
    logger.info(f"  ① video: {video_path}")
    logger.info(f"  ② audio: {audio_path}")
    logger.info(f"  ③ subtitle: {subtitle_path}")
    logger.info(f"  ④ output: {output_file}")

    # https://github.com/harry0703/MoneyPrinterTurbo/issues/217
    # PermissionError: [WinError 32] The process cannot access the file because it is being used by another process: 'final-1.mp4.tempTEMP_MPY_wvf_snd.mp3'
    # write into the same directory as the output file
    output_dir = os.path.dirname(output_file)

    font_path = ""
    if params.subtitle_enabled:
        if not params.font_name:
            params.font_name = "STHeitiMedium.ttc"
        font_path = os.path.join(utils.font_dir(), params.font_name)
        if os.name == "nt":
            font_path = font_path.replace("\\", "/")

        logger.info(f"  ⑤ font: {font_path}")

    def create_text_clip(subtitle_item):
        params.font_size = int(params.font_size)
        params.stroke_width = int(params.stroke_width)
        phrase = subtitle_item[1]
        max_width = video_width * 0.9
        wrapped_txt, txt_height = wrap_text(
            phrase, max_width=max_width, font=font_path, fontsize=params.font_size
        )
        interline = int(params.font_size * 0.25)
        size=(int(max_width), int(txt_height + params.font_size * 0.25 + (interline * (wrapped_txt.count("\n") + 1))))

        _bg = params.text_background_color
        _bg = None if (not _bg or _bg == "transparent") else _bg
        _clip = TextClip(
            text=wrapped_txt,
            font=font_path,
            font_size=params.font_size,
            color=params.text_fore_color,
            bg_color=_bg,
            stroke_color=params.stroke_color,
            stroke_width=params.stroke_width,
            # interline=interline,
            # size=size,
        )
        duration = subtitle_item[0][1] - subtitle_item[0][0]
        _clip = _clip.with_start(subtitle_item[0][0])
        _clip = _clip.with_end(subtitle_item[0][1])
        _clip = _clip.with_duration(duration)
        if params.subtitle_position == "bottom":
            _clip = _clip.with_position(("center", video_height * 0.95 - _clip.h))
        elif params.subtitle_position == "top":
            _clip = _clip.with_position(("center", video_height * 0.05))
        elif params.subtitle_position == "custom":
            # Ensure the subtitle is fully within the screen bounds
            margin = 10  # Additional margin, in pixels
            max_y = video_height - _clip.h - margin
            min_y = margin
            custom_y = (video_height - _clip.h) * (params.custom_position / 100)
            custom_y = max(
                min_y, min(custom_y, max_y)
            )  # Constrain the y value within the valid range
            _clip = _clip.with_position(("center", custom_y))
        else:  # center
            _clip = _clip.with_position(("center", "center"))
        return _clip

    video_clip = VideoFileClip(video_path).without_audio()
    audio_clip = AudioFileClip(audio_path).with_effects(
        [afx.MultiplyVolume(params.voice_volume)]
    )

    def make_textclip(text):
        return TextClip(
            text=text,
            font=font_path,
            font_size=params.font_size,
        )

    if subtitle_path and os.path.exists(subtitle_path):
        sub = SubtitlesClip(
            subtitles=subtitle_path, encoding="utf-8", make_textclip=make_textclip
        )
        text_clips = []
        for item in sub.subtitles:
            clip = create_text_clip(subtitle_item=item)
            text_clips.append(clip)
        video_clip = CompositeVideoClip([video_clip, *text_clips])

    bgm_file = get_bgm_file(bgm_type=params.bgm_type, bgm_file=params.bgm_file)
    if bgm_file:
        try:
            bgm_clip = AudioFileClip(bgm_file).with_effects(
                [
                    afx.MultiplyVolume(params.bgm_volume),
                    afx.AudioFadeOut(3),
                    afx.AudioLoop(duration=video_clip.duration),
                ]
            )
            audio_clip = CompositeAudioClip([audio_clip, bgm_clip])
        except Exception as e:
            logger.error(f"failed to add bgm: {str(e)}")

    video_clip = video_clip.with_audio(audio_clip)
    video_clip.write_videofile(
        output_file,
        audio_codec=audio_codec,
        temp_audiofile_path=output_dir,
        threads=params.n_threads or 2,
        logger=None,
        fps=fps,
    )
    video_clip.close()
    del video_clip


_DEMUCS_MODEL = None


def _strip_vocals(clip_path, work_dir):
    """用 demucs 從影片音軌分離出「無人聲」的純環境音（去掉 Veo 生成的角色
    對白語音），回傳環境音 wav 路徑；失敗回 None。供 TTS 配音模式使用，避免
    Veo 對白與 TTS 配音疊音，只保留環境音效當背景。
    走 demucs python API + soundfile 存檔（繞開 torchaudio.save 對 torchcodec
    的依賴）。模型（htdemucs）快取於全域避免每段重載。"""
    global _DEMUCS_MODEL
    try:
        import torch
        import soundfile as sf
        from demucs.pretrained import get_model
        from demucs.apply import apply_model
        from demucs.audio import AudioFile, convert_audio
        if _DEMUCS_MODEL is None:
            _DEMUCS_MODEL = get_model("htdemucs")
            _DEMUCS_MODEL.eval()
        model = _DEMUCS_MODEL
        # 讀取影片音軌（AudioFile 走 ffmpeg 解碼，不依賴 torchaudio.load）
        wav = AudioFile(clip_path).read(streams=0, samplerate=model.samplerate,
                                        channels=model.audio_channels)
        ref = wav.mean(0)
        wav = (wav - ref.mean()) / (ref.std() + 1e-8)
        with torch.no_grad():
            sources = apply_model(model, wav[None], device="cpu", progress=False)[0]
        sources = sources * ref.std() + ref.mean()
        vi = model.sources.index("vocals")
        no_vocals = sum(sources[i] for i in range(len(model.sources)) if i != vi)
        out = os.path.join(work_dir, "_no_vocals.wav")
        sf.write(out, no_vocals.T.cpu().numpy(), model.samplerate)
        if os.path.exists(out):
            logger.info("demucs: Veo 對白人聲已移除，保留環境音效")
            return out
        return None
    except Exception as e:
        logger.warning(f"vocal-strip skipped: {e}")
        return None


def compose_segment_video(clip_path, audio_path, subtitle_path, output_file, params,
                          ambient_volume=0.12, min_duration=0, strip_vocals=False):
    """Compose ONE storyboard segment so it performs for the FULL narration:
    - the clip's motion plays once; if shorter than the voiceover it HOLDS the
      last frame (no 4-second looping); if longer it is trimmed to the voiceover.
    - audio = voiceover (main) + the clip's own sound mixed low as ambient
      (keeps Veo's scene/environment sound instead of discarding it).
    - subtitles are burned in. Segment length = voiceover length.
    """
    from moviepy import (VideoFileClip, AudioFileClip, CompositeAudioClip,
                         CompositeVideoClip, concatenate_videoclips, afx)
    aspect = VideoAspect(params.video_aspect)
    video_width, video_height = aspect.to_resolution()
    output_dir = os.path.dirname(output_file)

    src = VideoFileClip(clip_path)
    native_audio = src.audio  # Veo ambient sound (may be None for image clips)

    # 無旁白模式（audio_path 空）：只用畫面本身的原生音軌，長度=片段長度
    _has_voice = bool(audio_path) and os.path.exists(audio_path)
    if _has_voice:
        voice = AudioFileClip(audio_path).with_effects([afx.MultiplyVolume(params.voice_volume)])
        target = voice.duration
    else:
        voice = None
        target = src.duration
    # 讓段落至少達到設定的秒數（不足則定格延續）
    if min_duration and target < min_duration:
        target = float(min_duration)

    # resize/pad the visual to the target frame size
    vid = src.without_audio()
    if vid.size != [video_width, video_height] and tuple(vid.size) != (video_width, video_height):
        cr = vid.w / vid.h
        tr = video_width / video_height
        if abs(cr - tr) < 0.01:
            vid = vid.resized(new_size=(video_width, video_height))
        else:
            if cr > tr:
                sf = video_width / vid.w
            else:
                sf = video_height / vid.h
            nv = vid.resized(new_size=(int(vid.w * sf), int(vid.h * sf))).with_position("center")
            bg = ColorClip(size=(video_width, video_height), color=(0, 0, 0)).with_duration(vid.duration)
            vid = CompositeVideoClip([bg, nv])

    # match the visual length to the voiceover: trim if longer, freeze-hold if shorter
    _freeze_extra = 0.0
    if vid.duration >= target:
        base_video = vid.subclipped(0, target)
    else:
        # 需要定格延續：先把畫面本身寫成乾淨暫存檔（避免直接讀原檔末端越界），
        # 再用 ffmpeg tpad 保持最後一幀延長到目標長度（快、穩定）。
        _freeze_extra = target - vid.duration
        base_video = vid.subclipped(0, vid.duration)  # 完整畫面，長度=片段本身
        target = vid.duration  # 先合成到片段長度，延長在最後用 ffmpeg 做
    base_video = base_video.with_duration(target)

    # audio
    if _has_voice:
        # 有旁白（TTS 配音）：旁白為主 + 環境音混入。strip_vocals 時先用 demucs
        # 移除 Veo 影片中的角色對白人聲、只留環境音效，避免與 TTS 配音疊音。
        audio_layers = [voice]
        if native_audio is not None:
            try:
                _amb_path = _strip_vocals(clip_path, output_dir) if strip_vocals else None
                if _amb_path:
                    _af = AudioFileClip(_amb_path)
                    # 已去人聲 → 純環境音，無疊音風險，音量可提高讓環境音效清楚
                    amb = _af.subclipped(0, min(_af.duration, target)).with_effects(
                        [afx.MultiplyVolume(0.6)])
                else:
                    amb = native_audio.subclipped(0, min(native_audio.duration, target)).with_effects(
                        [afx.MultiplyVolume(ambient_volume)])
                audio_layers.append(amb)
            except Exception as e:
                logger.warning(f"ambient audio skipped: {e}")
        base_video = base_video.with_audio(CompositeAudioClip(audio_layers))
    elif native_audio is not None:
        # 無旁白：直接用畫面原生音軌（正常音量）
        try:
            base_video = base_video.with_audio(native_audio.subclipped(0, min(native_audio.duration, target)))
        except Exception as e:
            logger.warning(f"native audio kept-raw failed: {e}")

    # subtitles
    if params.subtitle_enabled and subtitle_path and os.path.exists(subtitle_path):
        font_path = os.path.join(utils.font_dir(), params.font_name or "MicrosoftYaHeiBold.ttc")
        try:
            def _mk(text):
                return TextClip(text=text, font=font_path, font_size=int(params.font_size))
            sub = SubtitlesClip(subtitles=subtitle_path, encoding="utf-8", make_textclip=_mk)
            tclips = []
            for (ts, te), phrase in sub.subtitles:
                mw = video_width * 0.9
                wrapped, th = wrap_text(phrase, max_width=mw, font=font_path, fontsize=params.font_size)
                # moviepy 2.x TextClip 的 bg_color 不接受 'transparent'（PIL 顏色），
                # 空值/transparent 一律轉 None（透明底），否則字幕整段拋錯不顯示
                _bg = params.text_background_color
                _bg = None if (not _bg or _bg == "transparent") else _bg
                tc = TextClip(text=wrapped, font=font_path, font_size=int(params.font_size),
                              color=params.text_fore_color, bg_color=_bg,
                              stroke_color=params.stroke_color, stroke_width=int(params.stroke_width))
                tc = tc.with_start(ts).with_end(te).with_duration(te - ts)
                if params.subtitle_position == "top":
                    tc = tc.with_position(("center", video_height * 0.05))
                elif params.subtitle_position == "center":
                    tc = tc.with_position(("center", "center"))
                else:
                    tc = tc.with_position(("center", video_height * 0.95 - tc.h))
                tclips.append(tc)
            base_video = CompositeVideoClip([base_video, *tclips])
        except Exception as e:
            logger.error(f"subtitle overlay failed: {e}")

    base_video = base_video.with_duration(target)
    # 需要延續到設定秒數 → 先寫暫存檔，再用 ffmpeg tpad 保持最後一幀延長
    if _freeze_extra > 0.05:
        tmp_out = output_file + ".base.mp4"
        base_video.write_videofile(tmp_out, audio_codec=audio_codec,
                                   temp_audiofile_path=output_dir,
                                   threads=params.n_threads or 2, logger=None, fps=fps)
        close_clip(src)
        close_clip(base_video)
        try:
            import imageio_ffmpeg
            _ff = imageio_ffmpeg.get_ffmpeg_exe()
            import subprocess as _sp
            _sp.run([_ff, "-y", "-i", tmp_out,
                     "-vf", f"tpad=stop_mode=clone:stop_duration={_freeze_extra:.2f}",
                     "-af", f"apad=pad_dur={_freeze_extra:.2f}",
                     "-c:v", "libx264", "-c:a", "aac", output_file],
                    capture_output=True, check=True)
            os.remove(tmp_out)
        except Exception as e:
            logger.error(f"freeze-extend failed, using base: {e}")
            if os.path.exists(tmp_out):
                shutil.move(tmp_out, output_file)
        logger.success(f"segment composed ({target + _freeze_extra:.1f}s, freeze-hold): {output_file}")
    else:
        base_video.write_videofile(output_file, audio_codec=audio_codec,
                                   temp_audiofile_path=output_dir,
                                   threads=params.n_threads or 2, logger=None, fps=fps)
        close_clip(src)
        close_clip(base_video)
        logger.success(f"segment composed ({target:.1f}s): {output_file}")
    return output_file


def merge_segment_videos(segment_files: List[str], output_file: str, params,
                         transitions: List[str] = None):
    """Concatenate reviewed segment videos (voice + subtitles already burned in)
    into the final video. transitions[i] defines how the film enters segment i
    ("none" | "fade_in" | "fade" | "slide_in"), driven by each segment's
    connecting instruction. Background music is mixed over the result."""
    clips = [VideoFileClip(f) for f in segment_files]
    transitions = transitions or []
    crossfade = float(getattr(params, "crossfade", 0) or 0)

    if crossfade > 0 and len(clips) > 1:
        # 溶接串場：每段溶入下一段（overlap dissolve），像專業串場
        from moviepy import vfx as _vfx
        cf = min(crossfade, 1.2)
        comp = [clips[0]]
        for i in range(1, len(clips)):
            comp.append(clips[i].with_effects([_vfx.CrossFadeIn(cf)]))
        merged = concatenate_videoclips(comp, method="compose", padding=-cf)
        logger.info(f"merged with crossfade dissolve {cf}s between {len(clips)} segments")
    else:
        for i in range(len(clips)):
            fx = transitions[i] if i < len(transitions) else "none"
            try:
                if fx == "fade_in":
                    clips[i] = video_effects.fadein_transition(clips[i], 1.0)
                elif fx == "fade" and i > 0:
                    # dip to black: previous clip fades out, this one fades in
                    clips[i - 1] = video_effects.fadeout_transition(clips[i - 1], 0.8)
                    clips[i] = video_effects.fadein_transition(clips[i], 0.8)
                elif fx == "slide_in" and i > 0:
                    clips[i] = video_effects.slidein_transition(clips[i], 1.0, "left")
            except Exception as e:
                logger.warning(f"transition '{fx}' on segment {i + 1} failed: {e}")
        merged = concatenate_videoclips(clips)
    audio_clip = merged.audio
    bgm_file = get_bgm_file(bgm_type=params.bgm_type, bgm_file=params.bgm_file)
    if bgm_file and audio_clip is not None:
        try:
            bgm_clip = AudioFileClip(bgm_file)
            # loop bgm to cover the whole film (AudioLoop 在部分 moviepy 版本不可用 → fallback)
            try:
                bgm_clip = bgm_clip.with_effects([afx.AudioLoop(duration=merged.duration)])
            except Exception:
                import math as _m
                from moviepy import concatenate_audioclips as _cat
                n = max(1, _m.ceil(merged.duration / max(0.1, bgm_clip.duration)))
                bgm_clip = _cat([bgm_clip] * n).subclipped(0, merged.duration)
            bgm_clip = bgm_clip.with_effects([afx.MultiplyVolume(params.bgm_volume or 0.2),
                                              afx.AudioFadeOut(3)])
            merged = merged.with_audio(CompositeAudioClip([audio_clip, bgm_clip]))
            logger.info(f"bgm added: {os.path.basename(bgm_file)} vol={params.bgm_volume}")
        except Exception as e:
            logger.error(f"failed to add bgm: {str(e)}")
    else:
        logger.warning(f"no bgm (bgm_file={bool(bgm_file)}, audio={audio_clip is not None})")
    merged.write_videofile(
        output_file,
        audio_codec="aac",
        temp_audiofile_path=os.path.dirname(output_file),
        threads=params.n_threads or 2,
        logger=None,
        fps=30,
    )
    for c in clips:
        close_clip(c)
    logger.success(f"merged {len(segment_files)} segments -> {output_file}")
    return output_file


def preprocess_video(materials: List[MaterialInfo], clip_duration=4):
    for material in materials:
        if not material.url:
            continue

        ext = utils.parse_extension(material.url)
        try:
            clip = VideoFileClip(material.url)
        except Exception:
            clip = ImageClip(material.url)

        width = clip.size[0]
        height = clip.size[1]
        if width < 480 or height < 480:
            logger.warning(f"low resolution material: {width}x{height}, minimum 480x480 required")
            continue

        if ext in const.FILE_TYPE_IMAGES:
            logger.info(f"processing image: {material.url}")
            # Create an image clip and set its duration to 3 seconds
            clip = (
                ImageClip(material.url)
                .with_duration(clip_duration)
                .with_position("center")
            )
            # Apply a zoom effect using the resize method.
            # A lambda function is used to make the zoom effect dynamic over time.
            # The zoom effect starts from the original size and gradually scales up to 120%.
            # t represents the current time, and clip.duration is the total duration of the clip (3 seconds).
            # Note: 1 represents 100% size, so 1.2 represents 120% size.
            zoom_clip = clip.resized(
                lambda t: 1 + (clip_duration * 0.03) * (t / clip.duration)
            )

            # Optionally, create a composite video clip containing the zoomed clip.
            # This is useful when you want to add other elements to the video.
            final_clip = CompositeVideoClip([zoom_clip])

            # Output the video to a file.
            video_file = f"{material.url}.mp4"
            final_clip.write_videofile(video_file, fps=30, logger=None)
            close_clip(clip)
            material.url = video_file
            logger.success(f"image processed: {video_file}")
    return materials