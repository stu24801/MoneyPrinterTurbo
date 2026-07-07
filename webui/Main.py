import hashlib
import json
import math
import os
import platform
import re
import sys
from uuid import uuid4

import streamlit as st
from loguru import logger

# Add the root directory of the project to the system path to allow importing modules from the project
root_dir = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
if root_dir not in sys.path:
    sys.path.append(root_dir)
    print("******** sys.path ********")
    print(sys.path)
    print("")

from app.config import config
from app.models.schema import (
    MaterialInfo,
    VideoAspect,
    VideoConcatMode,
    VideoParams,
    VideoTransitionMode,
)
from app.services import llm, material, video, voice
from app.services import task as tm
from app.utils import utils

st.set_page_config(
    page_title="短片生成器",
    page_icon="🎬",
    layout="wide",
    initial_sidebar_state="auto",
    menu_items={
        "Report a bug": "https://github.com/harry0703/MoneyPrinterTurbo/issues",
        "About": "# MoneyPrinterTurbo\nSimply provide a topic or keyword for a video, and it will "
        "automatically generate the video copy, video materials, video subtitles, "
        "and video background music before synthesizing a high-definition short "
        "video.\n\nhttps://github.com/harry0703/MoneyPrinterTurbo",
    },
)


streamlit_style = """
<style>
h1 {
    padding-top: 0 !important;
}
</style>
"""
st.markdown(streamlit_style, unsafe_allow_html=True)

from webui import auth

auth.require_login()
auth.render_user_sidebar()

# 定义资源目录
font_dir = os.path.join(root_dir, "resource", "fonts")
song_dir = os.path.join(root_dir, "resource", "songs")
i18n_dir = os.path.join(root_dir, "webui", "i18n")
config_file = os.path.join(root_dir, "webui", ".streamlit", "webui.toml")
system_locale = utils.get_system_locale()


if "video_subject" not in st.session_state:
    st.session_state["video_subject"] = ""
if "video_script" not in st.session_state:
    st.session_state["video_script"] = ""
if "video_terms" not in st.session_state:
    st.session_state["video_terms"] = ""
if "ui_language" not in st.session_state:
    st.session_state["ui_language"] = config.ui.get("language", system_locale)

# 套用「從產製歷史載入」的待載入資料——必須在表單元件渲染前執行，
# 否則帶 key 的 widget（如 video_subject_input）無法被程式設定。
_pending = st.session_state.pop("_pending_load", None)
if _pending:
    st.session_state["video_subject"] = _pending.get("subject", "")
    st.session_state["video_subject_input"] = _pending.get("subject", "")
    st.session_state["video_script"] = _pending.get("script", "")
    _terms = _pending.get("terms", "")
    if isinstance(_terms, list):
        _terms = ", ".join(str(t) for t in _terms)
    st.session_state["video_terms"] = _terms

# 加载语言文件
locales = utils.load_locales(i18n_dir)

# 创建一个顶部栏，包含标题和语言选择
title_col, lang_col = st.columns([3, 1])

with title_col:
    st.title(f"🎬 短片生成器 v{config.project_version}")

with lang_col:
    display_languages = []
    selected_index = 0
    for i, code in enumerate(locales.keys()):
        display_languages.append(f"{code} - {locales[code].get('Language')}")
        if code == st.session_state.get("ui_language", ""):
            selected_index = i

    selected_language = st.selectbox(
        "Language / 语言",
        options=display_languages,
        index=selected_index,
        key="top_language_selector",
        label_visibility="collapsed",
    )
    if selected_language:
        code = selected_language.split(" - ")[0].strip()
        st.session_state["ui_language"] = code
        config.ui["language"] = code

support_locales = [
    "zh-CN",
    "zh-HK",
    "zh-TW",
    "de-DE",
    "en-US",
    "fr-FR",
    "vi-VN",
    "th-TH",
    "tr-TR",
]


def get_all_fonts():
    fonts = []
    for root, dirs, files in os.walk(font_dir):
        for file in files:
            if file.endswith(".ttf") or file.endswith(".ttc"):
                fonts.append(file)
    fonts.sort()
    return fonts


def get_all_songs():
    songs = []
    for root, dirs, files in os.walk(song_dir):
        for file in files:
            if file.endswith(".mp3"):
                songs.append(file)
    return songs


def open_task_folder(task_id):
    try:
        sys = platform.system()
        path = os.path.join(root_dir, "storage", "tasks", task_id)
        if os.path.exists(path):
            if sys == "Windows":
                os.system(f"start {path}")
            if sys == "Darwin":
                os.system(f"open {path}")
    except Exception as e:
        logger.error(e)


def scroll_to_bottom():
    js = """
    <script>
        console.log("scroll_to_bottom");
        function scroll(dummy_var_to_force_repeat_execution){
            var sections = parent.document.querySelectorAll('section.main');
            console.log(sections);
            for(let index = 0; index<sections.length; index++) {
                sections[index].scrollTop = sections[index].scrollHeight;
            }
        }
        scroll(1);
    </script>
    """
    st.components.v1.html(js, height=0, width=0)


def init_log():
    logger.remove()
    _lvl = "DEBUG"

    def format_record(record):
        # 获取日志记录中的文件全路径
        file_path = record["file"].path
        # 将绝对路径转换为相对于项目根目录的路径
        relative_path = os.path.relpath(file_path, root_dir)
        # 更新记录中的文件路径
        record["file"].path = f"./{relative_path}"
        # 返回修改后的格式字符串
        # 您可以根据需要调整这里的格式
        record["message"] = record["message"].replace(root_dir, ".")

        _format = (
            "<green>{time:%Y-%m-%d %H:%M:%S}</> | "
            + "<level>{level}</> | "
            + '"{file.path}:{line}":<blue> {function}</> '
            + "- <level>{message}</>"
            + "\n"
        )
        return _format

    logger.add(
        sys.stdout,
        level=_lvl,
        format=format_record,
        colorize=True,
    )


init_log()

locales = utils.load_locales(i18n_dir)


def tr(key):
    loc = locales.get(st.session_state["ui_language"], {})
    return loc.get("Translation", {}).get(key, key)


# 创建基础设置折叠框
if not config.app.get("hide_config", False):
    with st.expander(tr("Basic Settings"), expanded=False):
        config_panels = st.columns(3)
        left_config_panel = config_panels[0]
        middle_config_panel = config_panels[1]
        right_config_panel = config_panels[2]

        # 左侧面板 - 日志设置
        with left_config_panel:
            # 是否隐藏配置面板
            hide_config = st.checkbox(
                tr("Hide Basic Settings"), value=config.app.get("hide_config", False)
            )
            config.app["hide_config"] = hide_config

            # 是否禁用日志显示
            hide_log = st.checkbox(
                tr("Hide Log"), value=config.ui.get("hide_log", False)
            )
            config.ui["hide_log"] = hide_log

        # 中间面板 - LLM 设置

        with middle_config_panel:
            st.write(tr("LLM Settings"))
            llm_providers = [
                "OpenAI",
                "Moonshot",
                "Azure",
                "Qwen",
                "DeepSeek",
                "ModelScope",
                "Gemini",
                "Ollama",
                "G4f",
                "OneAPI",
                "Cloudflare",
                "ERNIE",
                "Pollinations",
            ]
            saved_llm_provider = config.app.get("llm_provider", "OpenAI").lower()
            saved_llm_provider_index = 0
            for i, provider in enumerate(llm_providers):
                if provider.lower() == saved_llm_provider:
                    saved_llm_provider_index = i
                    break

            llm_provider = st.selectbox(
                tr("LLM Provider"),
                options=llm_providers,
                index=saved_llm_provider_index,
            )
            llm_helper = st.container()
            llm_provider = llm_provider.lower()
            config.app["llm_provider"] = llm_provider

            llm_api_key = config.app.get(f"{llm_provider}_api_key", "")
            llm_secret_key = config.app.get(
                f"{llm_provider}_secret_key", ""
            )  # only for baidu ernie
            llm_base_url = config.app.get(f"{llm_provider}_base_url", "")
            llm_model_name = config.app.get(f"{llm_provider}_model_name", "")
            llm_account_id = config.app.get(f"{llm_provider}_account_id", "")

            tips = ""
            if llm_provider == "ollama":
                if not llm_model_name:
                    llm_model_name = "qwen:7b"
                if not llm_base_url:
                    llm_base_url = "http://localhost:11434/v1"

                with llm_helper:
                    tips = """
                            ##### Ollama配置说明
                            - **API Key**: 随便填写，比如 123
                            - **Base Url**: 一般为 http://localhost:11434/v1
                                - 如果 `MoneyPrinterTurbo` 和 `Ollama` **不在同一台机器上**，需要填写 `Ollama` 机器的IP地址
                                - 如果 `MoneyPrinterTurbo` 是 `Docker` 部署，建议填写 `http://host.docker.internal:11434/v1`
                            - **Model Name**: 使用 `ollama list` 查看，比如 `qwen:7b`
                            """

            if llm_provider == "openai":
                if not llm_model_name:
                    llm_model_name = "gpt-3.5-turbo"
                with llm_helper:
                    tips = """
                            ##### OpenAI 配置说明
                            > 需要VPN开启全局流量模式
                            - **API Key**: [点击到官网申请](https://platform.openai.com/api-keys)
                            - **Base Url**: 可以留空
                            - **Model Name**: 填写**有权限**的模型，[点击查看模型列表](https://platform.openai.com/settings/organization/limits)
                            """

            if llm_provider == "moonshot":
                if not llm_model_name:
                    llm_model_name = "moonshot-v1-8k"
                with llm_helper:
                    tips = """
                            ##### Moonshot 配置说明
                            - **API Key**: [点击到官网申请](https://platform.moonshot.cn/console/api-keys)
                            - **Base Url**: 固定为 https://api.moonshot.cn/v1
                            - **Model Name**: 比如 moonshot-v1-8k，[点击查看模型列表](https://platform.moonshot.cn/docs/intro#%E6%A8%A1%E5%9E%8B%E5%88%97%E8%A1%A8)
                            """
            if llm_provider == "oneapi":
                if not llm_model_name:
                    llm_model_name = (
                        "claude-3-5-sonnet-20240620"  # 默认模型，可以根据需要调整
                    )
                with llm_helper:
                    tips = """
                        ##### OneAPI 配置说明
                        - **API Key**: 填写您的 OneAPI 密钥
                        - **Base Url**: 填写 OneAPI 的基础 URL
                        - **Model Name**: 填写您要使用的模型名称，例如 claude-3-5-sonnet-20240620
                        """

            if llm_provider == "qwen":
                if not llm_model_name:
                    llm_model_name = "qwen-max"
                with llm_helper:
                    tips = """
                            ##### 通义千问Qwen 配置说明
                            - **API Key**: [点击到官网申请](https://dashscope.console.aliyun.com/apiKey)
                            - **Base Url**: 留空
                            - **Model Name**: 比如 qwen-max，[点击查看模型列表](https://help.aliyun.com/zh/dashscope/developer-reference/model-introduction#3ef6d0bcf91wy)
                            """

            if llm_provider == "g4f":
                if not llm_model_name:
                    llm_model_name = "gpt-3.5-turbo"
                with llm_helper:
                    tips = """
                            ##### gpt4free 配置说明
                            > [GitHub开源项目](https://github.com/xtekky/gpt4free)，可以免费使用GPT模型，但是**稳定性较差**
                            - **API Key**: 随便填写，比如 123
                            - **Base Url**: 留空
                            - **Model Name**: 比如 gpt-3.5-turbo，[点击查看模型列表](https://github.com/xtekky/gpt4free/blob/main/g4f/models.py#L308)
                            """
            if llm_provider == "azure":
                with llm_helper:
                    tips = """
                            ##### Azure 配置说明
                            > [点击查看如何部署模型](https://learn.microsoft.com/zh-cn/azure/ai-services/openai/how-to/create-resource)
                            - **API Key**: [点击到Azure后台创建](https://portal.azure.com/#view/Microsoft_Azure_ProjectOxford/CognitiveServicesHub/~/OpenAI)
                            - **Base Url**: 留空
                            - **Model Name**: 填写你实际的部署名
                            """

            if llm_provider == "gemini":
                if not llm_model_name:
                    llm_model_name = "gemini-1.0-pro"

                with llm_helper:
                    tips = """
                            ##### Gemini 配置说明
                            > 需要VPN开启全局流量模式
                            - **API Key**: [点击到官网申请](https://ai.google.dev/)
                            - **Base Url**: 留空
                            - **Model Name**: 比如 gemini-1.0-pro
                            """

            if llm_provider == "deepseek":
                if not llm_model_name:
                    llm_model_name = "deepseek-chat"
                if not llm_base_url:
                    llm_base_url = "https://api.deepseek.com"
                with llm_helper:
                    tips = """
                            ##### DeepSeek 配置说明
                            - **API Key**: [点击到官网申请](https://platform.deepseek.com/api_keys)
                            - **Base Url**: 固定为 https://api.deepseek.com
                            - **Model Name**: 固定为 deepseek-chat
                            """

            if llm_provider == "modelscope":
                if not llm_model_name:
                    llm_model_name = "Qwen/Qwen3-32B"
                if not llm_base_url:
                    llm_base_url = "https://api-inference.modelscope.cn/v1/"
                with llm_helper:
                    tips = """
                            ##### ModelScope 配置说明
                            - **API Key**: [点击到官网申请](https://modelscope.cn/docs/model-service/API-Inference/intro)
                            - **Base Url**: 固定为 https://api-inference.modelscope.cn/v1/
                            - **Model Name**: 比如 Qwen/Qwen3-32B，[点击查看模型列表](https://modelscope.cn/models?filter=inference_type&page=1)
                            """

            if llm_provider == "ernie":
                with llm_helper:
                    tips = """
                            ##### 百度文心一言 配置说明
                            - **API Key**: [点击到官网申请](https://console.bce.baidu.com/qianfan/ais/console/applicationConsole/application)
                            - **Secret Key**: [点击到官网申请](https://console.bce.baidu.com/qianfan/ais/console/applicationConsole/application)
                            - **Base Url**: 填写 **请求地址** [点击查看文档](https://cloud.baidu.com/doc/WENXINWORKSHOP/s/jlil56u11#%E8%AF%B7%E6%B1%82%E8%AF%B4%E6%98%8E)
                            """

            if llm_provider == "pollinations":
                if not llm_model_name:
                    llm_model_name = "default"
                with llm_helper:
                    tips = """
                            ##### Pollinations AI Configuration
                            - **API Key**: Optional - Leave empty for public access
                            - **Base Url**: Default is https://text.pollinations.ai/openai
                            - **Model Name**: Use 'openai-fast' or specify a model name
                            """

            if tips and config.ui["language"] == "zh":
                st.warning(
                    "中国用户建议使用 **DeepSeek** 或 **Moonshot** 作为大模型提供商\n- 国内可直接访问，不需要VPN \n- 注册就送额度，基本够用"
                )
                st.info(tips)

            st_llm_api_key = st.text_input(
                tr("API Key"), value=llm_api_key, type="password"
            )
            st_llm_base_url = st.text_input(tr("Base Url"), value=llm_base_url)
            st_llm_model_name = ""
            if llm_provider != "ernie":
                st_llm_model_name = st.text_input(
                    tr("Model Name"),
                    value=llm_model_name,
                    key=f"{llm_provider}_model_name_input",
                )
                if st_llm_model_name:
                    config.app[f"{llm_provider}_model_name"] = st_llm_model_name
            else:
                st_llm_model_name = None

            if st_llm_api_key:
                config.app[f"{llm_provider}_api_key"] = st_llm_api_key
            if st_llm_base_url:
                config.app[f"{llm_provider}_base_url"] = st_llm_base_url
            if st_llm_model_name:
                config.app[f"{llm_provider}_model_name"] = st_llm_model_name
            if llm_provider == "ernie":
                st_llm_secret_key = st.text_input(
                    tr("Secret Key"), value=llm_secret_key, type="password"
                )
                config.app[f"{llm_provider}_secret_key"] = st_llm_secret_key

            if llm_provider == "cloudflare":
                st_llm_account_id = st.text_input(
                    tr("Account ID"), value=llm_account_id
                )
                if st_llm_account_id:
                    config.app[f"{llm_provider}_account_id"] = st_llm_account_id

        # 右侧面板 - API 密钥设置
        with right_config_panel:

            def get_keys_from_config(cfg_key):
                api_keys = config.app.get(cfg_key, [])
                if isinstance(api_keys, str):
                    api_keys = [api_keys]
                api_key = ", ".join(api_keys)
                return api_key

            def save_keys_to_config(cfg_key, value):
                value = value.replace(" ", "")
                if value:
                    config.app[cfg_key] = value.split(",")

            st.write(tr("Video Source Settings"))

            pexels_api_key = get_keys_from_config("pexels_api_keys")
            pexels_api_key = st.text_input(
                tr("Pexels API Key"), value=pexels_api_key, type="password"
            )
            save_keys_to_config("pexels_api_keys", pexels_api_key)

            pixabay_api_key = get_keys_from_config("pixabay_api_keys")
            pixabay_api_key = st.text_input(
                tr("Pixabay API Key"), value=pixabay_api_key, type="password"
            )
            save_keys_to_config("pixabay_api_keys", pixabay_api_key)

llm_provider = config.app.get("llm_provider", "").lower()
panel = st.columns(3)
left_panel = panel[0]
middle_panel = panel[1]
right_panel = panel[2]

params = VideoParams(video_subject="")
uploaded_files = []

with left_panel:
    with st.container(border=True):
        st.write(tr("Video Script Settings"))
        params.video_subject = st.text_input(
            tr("Video Subject"),
            value=st.session_state["video_subject"],
            key="video_subject_input",
        ).strip()

        video_languages = [
            (tr("Auto Detect"), ""),
        ]
        for code in support_locales:
            video_languages.append((code, code))

        # Default script language: zh-TW when the UI is in Traditional Chinese,
        # so generated scripts (and thus subtitles) are Traditional Chinese.
        default_lang_index = 0
        if st.session_state.get("ui_language", "").startswith("zh-TW"):
            default_lang_index = next(
                (i for i, v in enumerate(video_languages) if v[1] == "zh-TW"), 0
            )
        selected_index = st.selectbox(
            tr("Script Language"),
            index=default_lang_index,
            options=range(
                len(video_languages)
            ),  # Use the index as the internal option value
            format_func=lambda x: video_languages[x][
                0
            ],  # The label is displayed to the user
        )
        params.video_language = video_languages[selected_index][1]

        if st.button(
            tr("Generate Video Script and Keywords"), key="auto_generate_script"
        ):
            with st.spinner(tr("Generating Video Script and Keywords")):
                script = llm.generate_script(
                    video_subject=params.video_subject, language=params.video_language
                )
                terms = llm.generate_terms(params.video_subject, script)
                if "Error: " in script:
                    st.error(tr(script))
                elif "Error: " in terms:
                    st.error(tr(terms))
                else:
                    st.session_state["video_script"] = script
                    st.session_state["video_terms"] = ", ".join(terms)
        params.video_script = st.text_area(
            tr("Video Script"), value=st.session_state["video_script"], height=280
        )
        if st.button(tr("Generate Video Keywords"), key="auto_generate_terms"):
            if not params.video_script:
                st.error(tr("Please Enter the Video Subject"))
                st.stop()

            with st.spinner(tr("Generating Video Keywords")):
                terms = llm.generate_terms(params.video_subject, params.video_script)
                if "Error: " in terms:
                    st.error(tr(terms))
                else:
                    st.session_state["video_terms"] = ", ".join(terms)

        params.video_terms = st.text_area(
            tr("Video Keywords"), value=st.session_state["video_terms"]
        )

with middle_panel:
    with st.expander("🎬 " + tr("Video Settings"), expanded=False):
        video_concat_modes = [
            (tr("Sequential"), "sequential"),
            (tr("Random"), "random"),
        ]
        video_sources = [
            (tr("AI Generated Images"), "llm"),
            (tr("AI Generated Videos"), "llm-video"),
            (tr("Pexels"), "pexels"),
            (tr("Pixabay"), "pixabay"),
            (tr("Local file"), "local"),
            (tr("TikTok"), "douyin"),
            (tr("Bilibili"), "bilibili"),
            (tr("Xiaohongshu"), "xiaohongshu"),
        ]

        saved_video_source_name = config.app.get("video_source", "pexels")
        saved_video_source_index = [v[1] for v in video_sources].index(
            saved_video_source_name
        )

        selected_index = st.selectbox(
            tr("Video Source"),
            options=range(len(video_sources)),
            format_func=lambda x: video_sources[x][0],
            index=saved_video_source_index,
        )
        params.video_source = video_sources[selected_index][1]
        config.app["video_source"] = params.video_source

        if params.video_source == "local":
            uploaded_files = st.file_uploader(
                "Upload Local Files",
                type=["mp4", "mov", "avi", "flv", "mkv", "jpg", "jpeg", "png"],
                accept_multiple_files=True,
            )

        selected_index = st.selectbox(
            tr("Video Concat Mode"),
            index=1,
            options=range(
                len(video_concat_modes)
            ),  # Use the index as the internal option value
            format_func=lambda x: video_concat_modes[x][
                0
            ],  # The label is displayed to the user
        )
        params.video_concat_mode = VideoConcatMode(
            video_concat_modes[selected_index][1]
        )

        # 视频转场模式
        video_transition_modes = [
            (tr("None"), VideoTransitionMode.none.value),
            (tr("Shuffle"), VideoTransitionMode.shuffle.value),
            (tr("FadeIn"), VideoTransitionMode.fade_in.value),
            (tr("FadeOut"), VideoTransitionMode.fade_out.value),
            (tr("SlideIn"), VideoTransitionMode.slide_in.value),
            (tr("SlideOut"), VideoTransitionMode.slide_out.value),
        ]
        selected_index = st.selectbox(
            tr("Video Transition Mode"),
            options=range(len(video_transition_modes)),
            format_func=lambda x: video_transition_modes[x][0],
            index=0,
        )
        params.video_transition_mode = VideoTransitionMode(
            video_transition_modes[selected_index][1]
        )

        video_aspect_ratios = [
            (tr("Portrait"), VideoAspect.portrait.value),
            (tr("Landscape"), VideoAspect.landscape.value),
        ]
        selected_index = st.selectbox(
            tr("Video Ratio"),
            options=range(
                len(video_aspect_ratios)
            ),  # Use the index as the internal option value
            format_func=lambda x: video_aspect_ratios[x][
                0
            ],  # The label is displayed to the user
        )
        params.video_aspect = VideoAspect(video_aspect_ratios[selected_index][1])

        params.video_clip_duration = st.selectbox(
            tr("Clip Duration"), options=[2, 3, 4, 5, 6, 7, 8, 9, 10], index=1
        )
        _pm_opts = ["narration", "drama"]
        _saved_pm = config.ui.get("presentation_mode", "narration")
        params.presentation_mode = st.selectbox(
            tr("Presentation Mode"),
            options=_pm_opts,
            index=_pm_opts.index(_saved_pm) if _saved_pm in _pm_opts else 0,
            format_func=lambda x: tr("Narration mode") if x == "narration" else tr("Drama mode"),
        )
        config.ui["presentation_mode"] = params.presentation_mode
        _dur_opts = [0, 30, 45, 60, 90, 120, 180]
        _saved_total = config.ui.get("video_total_duration", 60)
        params.video_total_duration = st.selectbox(
            tr("Total Video Duration"),
            options=_dur_opts,
            index=_dur_opts.index(_saved_total) if _saved_total in _dur_opts else 3,
            format_func=lambda x: tr("Auto by script") if x == 0 else f"{x} " + tr("seconds"),
        )
        config.ui["video_total_duration"] = params.video_total_duration
        params.video_count = st.selectbox(
            tr("Number of Videos Generated Simultaneously"),
            options=[1, 2, 3, 4, 5],
            index=0,
        )
    with st.expander("🎧 " + tr("Audio Settings"), expanded=False):

        # 添加TTS服务器选择下拉框
        tts_servers = [
            ("azure-tts-v1", "Azure TTS V1"),
            ("azure-tts-v2", "Azure TTS V2"),
            ("siliconflow", "SiliconFlow TTS"),
            ("gemini-tts", "Google Gemini TTS"),
        ]
        # 只顯示已設定 API key 的伺服器（未設 key 的選了必失敗）
        _tts_available = {
            "azure-tts-v1": True,  # edge 免費，不需 key
            "azure-tts-v2": bool(config.azure.get("speech_key", "")),
            "siliconflow": bool(config.siliconflow.get("api_key", "")),
            "gemini-tts": bool(config.app.get("gemini_api_key", "")),
        }
        tts_servers = [s for s in tts_servers if _tts_available.get(s[0], False)]

        # 获取保存的TTS服务器，默认为v1
        saved_tts_server = config.ui.get("tts_server", "azure-tts-v1")
        saved_tts_server_index = 0
        for i, (server_value, _) in enumerate(tts_servers):
            if server_value == saved_tts_server:
                saved_tts_server_index = i
                break

        selected_tts_server_index = st.selectbox(
            tr("TTS Servers"),
            options=range(len(tts_servers)),
            format_func=lambda x: tts_servers[x][1],
            index=saved_tts_server_index,
        )

        selected_tts_server = tts_servers[selected_tts_server_index][0]
        config.ui["tts_server"] = selected_tts_server

        # 根据选择的TTS服务器获取声音列表
        filtered_voices = []

        if selected_tts_server == "siliconflow":
            # 获取硅基流动的声音列表
            filtered_voices = voice.get_siliconflow_voices()
        elif selected_tts_server == "gemini-tts":
            # 获取Gemini TTS的声音列表
            filtered_voices = voice.get_gemini_voices()
        else:
            # 获取Azure的声音列表
            all_voices = voice.get_all_azure_voices(filter_locals=None)

            # 根据选择的TTS服务器筛选声音
            for v in all_voices:
                if selected_tts_server == "azure-tts-v2":
                    # V2版本的声音名称中包含"v2"
                    if "V2" in v:
                        filtered_voices.append(v)
                else:
                    # V1版本的声音名称中不包含"v2"
                    if "V2" not in v:
                        filtered_voices.append(v)

        friendly_names = {
            v: v.replace("Female", tr("Female"))
            .replace("Male", tr("Male"))
            .replace("Neural", "")
            for v in filtered_voices
        }

        saved_voice_name = config.ui.get("voice_name", "")
        saved_voice_name_index = 0

        # 检查保存的声音是否在当前筛选的声音列表中
        if saved_voice_name in friendly_names:
            saved_voice_name_index = list(friendly_names.keys()).index(saved_voice_name)
        else:
            # 如果不在，则根据当前UI语言选择一个默认声音
            for i, v in enumerate(filtered_voices):
                if v.lower().startswith(st.session_state["ui_language"].lower()):
                    saved_voice_name_index = i
                    break

        # 如果没有找到匹配的声音，使用第一个声音
        if saved_voice_name_index >= len(friendly_names) and friendly_names:
            saved_voice_name_index = 0

        # 确保有声音可选
        if friendly_names:
            selected_friendly_name = st.selectbox(
                tr("Speech Synthesis"),
                options=list(friendly_names.values()),
                index=min(saved_voice_name_index, len(friendly_names) - 1)
                if friendly_names
                else 0,
            )

            voice_name = list(friendly_names.keys())[
                list(friendly_names.values()).index(selected_friendly_name)
            ]
            params.voice_name = voice_name
            config.ui["voice_name"] = voice_name
        else:
            # 如果没有声音可选，显示提示信息
            st.warning(
                tr(
                    "No voices available for the selected TTS server. Please select another server."
                )
            )
            params.voice_name = ""
            config.ui["voice_name"] = ""

        # 只有在有声音可选时才显示试听按钮
        if friendly_names and st.button(tr("Play Voice")):
            play_content = params.video_subject
            if not play_content:
                play_content = params.video_script
            if not play_content:
                play_content = tr("Voice Example")
            with st.spinner(tr("Synthesizing Voice")):
                temp_dir = utils.storage_dir("temp", create=True)
                audio_file = os.path.join(temp_dir, f"tmp-voice-{str(uuid4())}.mp3")
                sub_maker = voice.tts(
                    text=play_content,
                    voice_name=voice_name,
                    voice_rate=params.voice_rate,
                    voice_file=audio_file,
                    voice_volume=params.voice_volume,
                )
                # if the voice file generation failed, try again with a default content.
                if not sub_maker:
                    play_content = "This is a example voice. if you hear this, the voice synthesis failed with the original content."
                    sub_maker = voice.tts(
                        text=play_content,
                        voice_name=voice_name,
                        voice_rate=params.voice_rate,
                        voice_file=audio_file,
                        voice_volume=params.voice_volume,
                    )

                if sub_maker and os.path.exists(audio_file):
                    st.audio(audio_file, format="audio/mp3")
                    if os.path.exists(audio_file):
                        os.remove(audio_file)

        # 当选择V2版本或者声音是V2声音时，显示服务区域和API key输入框
        if selected_tts_server == "azure-tts-v2" or (
            voice_name and voice.is_azure_v2_voice(voice_name)
        ):
            saved_azure_speech_region = config.azure.get("speech_region", "")
            saved_azure_speech_key = config.azure.get("speech_key", "")
            azure_speech_region = st.text_input(
                tr("Speech Region"),
                value=saved_azure_speech_region,
                key="azure_speech_region_input",
            )
            azure_speech_key = st.text_input(
                tr("Speech Key"),
                value=saved_azure_speech_key,
                type="password",
                key="azure_speech_key_input",
            )
            config.azure["speech_region"] = azure_speech_region
            config.azure["speech_key"] = azure_speech_key

        # 当选择硅基流动时，显示API key输入框和说明信息
        if selected_tts_server == "siliconflow" or (
            voice_name and voice.is_siliconflow_voice(voice_name)
        ):
            saved_siliconflow_api_key = config.siliconflow.get("api_key", "")

            siliconflow_api_key = st.text_input(
                tr("SiliconFlow API Key"),
                value=saved_siliconflow_api_key,
                type="password",
                key="siliconflow_api_key_input",
            )

            # 显示硅基流动的说明信息
            st.info(
                tr("SiliconFlow TTS Settings")
                + ":\n"
                + "- "
                + tr("Speed: Range [0.25, 4.0], default is 1.0")
                + "\n"
                + "- "
                + tr("Volume: Uses Speech Volume setting, default 1.0 maps to gain 0")
            )

            config.siliconflow["api_key"] = siliconflow_api_key

        params.voice_volume = st.selectbox(
            tr("Speech Volume"),
            options=[0.6, 0.8, 1.0, 1.2, 1.5, 2.0, 3.0, 4.0, 5.0],
            index=2,
        )

        params.voice_rate = st.selectbox(
            tr("Speech Rate"),
            options=[0.8, 0.9, 1.0, 1.1, 1.2, 1.3, 1.5, 1.8, 2.0],
            index=2,
        )

        bgm_options = [
            (tr("No Background Music"), ""),
            (tr("Random Background Music"), "random"),
            (tr("Custom Background Music"), "custom"),
        ]
        selected_index = st.selectbox(
            tr("Background Music"),
            index=1,
            options=range(
                len(bgm_options)
            ),  # Use the index as the internal option value
            format_func=lambda x: bgm_options[x][
                0
            ],  # The label is displayed to the user
        )
        # Get the selected background music type
        params.bgm_type = bgm_options[selected_index][1]

        # Show or hide components based on the selection
        if params.bgm_type == "custom":
            custom_bgm_file = st.text_input(
                tr("Custom Background Music File"), key="custom_bgm_file_input"
            )
            if custom_bgm_file and os.path.exists(custom_bgm_file):
                params.bgm_file = custom_bgm_file
                # st.write(f":red[已选择自定义背景音乐]：**{custom_bgm_file}**")
        params.bgm_volume = st.selectbox(
            tr("Background Music Volume"),
            options=[0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0],
            index=2,
        )

with right_panel:
    with st.expander("💬 " + tr("Subtitle Settings"), expanded=False):
        params.subtitle_enabled = st.checkbox(tr("Enable Subtitles"), value=True)
        font_names = get_all_fonts()
        saved_font_name = config.ui.get("font_name", "MicrosoftYaHeiBold.ttc")
        saved_font_name_index = 0
        if saved_font_name in font_names:
            saved_font_name_index = font_names.index(saved_font_name)
        params.font_name = st.selectbox(
            tr("Font"), font_names, index=saved_font_name_index
        )
        config.ui["font_name"] = params.font_name

        subtitle_positions = [
            (tr("Top"), "top"),
            (tr("Center"), "center"),
            (tr("Bottom"), "bottom"),
            (tr("Custom"), "custom"),
        ]
        selected_index = st.selectbox(
            tr("Position"),
            index=2,
            options=range(len(subtitle_positions)),
            format_func=lambda x: subtitle_positions[x][0],
        )
        params.subtitle_position = subtitle_positions[selected_index][1]

        if params.subtitle_position == "custom":
            custom_position = st.text_input(
                tr("Custom Position (% from top)"),
                value="70.0",
                key="custom_position_input",
            )
            try:
                params.custom_position = float(custom_position)
                if params.custom_position < 0 or params.custom_position > 100:
                    st.error(tr("Please enter a value between 0 and 100"))
            except ValueError:
                st.error(tr("Please enter a valid number"))

        font_cols = st.columns([0.3, 0.7])
        with font_cols[0]:
            saved_text_fore_color = config.ui.get("text_fore_color", "#FFFFFF")
            params.text_fore_color = st.color_picker(
                tr("Font Color"), saved_text_fore_color
            )
            config.ui["text_fore_color"] = params.text_fore_color

        with font_cols[1]:
            saved_font_size = config.ui.get("font_size", 60)
            params.font_size = st.slider(tr("Font Size"), 30, 100, saved_font_size)
            config.ui["font_size"] = params.font_size

        stroke_cols = st.columns([0.3, 0.7])
        with stroke_cols[0]:
            params.stroke_color = st.color_picker(tr("Stroke Color"), "#000000")
        with stroke_cols[1]:
            params.stroke_width = st.slider(tr("Stroke Width"), 0.0, 10.0, 1.5)
    with st.expander(tr("Click to show API Key management"), expanded=False):
        st.subheader(tr("Manage Pexels and Pixabay API Keys"))

        col1, col2 = st.tabs(["Pexels API Keys", "Pixabay API Keys"])

        with col1:
            st.subheader("Pexels API Keys")
            if config.app["pexels_api_keys"]:
                st.write(tr("Current Keys:"))
                for key in config.app["pexels_api_keys"]:
                    st.code(key)
            else:
                st.info(tr("No Pexels API Keys currently"))

            new_key = st.text_input(tr("Add Pexels API Key"), key="pexels_new_key")
            if st.button(tr("Add Pexels API Key")):
                if new_key and new_key not in config.app["pexels_api_keys"]:
                    config.app["pexels_api_keys"].append(new_key)
                    config.save_config()
                    st.success(tr("Pexels API Key added successfully"))
                elif new_key in config.app["pexels_api_keys"]:
                    st.warning(tr("This API Key already exists"))
                else:
                    st.error(tr("Please enter a valid API Key"))

            if config.app["pexels_api_keys"]:
                delete_key = st.selectbox(
                    tr("Select Pexels API Key to delete"), config.app["pexels_api_keys"], key="pexels_delete_key"
                )
                if st.button(tr("Delete Selected Pexels API Key")):
                    config.app["pexels_api_keys"].remove(delete_key)
                    config.save_config()
                    st.success(tr("Pexels API Key deleted successfully"))

        with col2:
            st.subheader("Pixabay API Keys")

            if config.app["pixabay_api_keys"]:
                st.write(tr("Current Keys:"))
                for key in config.app["pixabay_api_keys"]:
                    st.code(key)
            else:
                st.info(tr("No Pixabay API Keys currently"))

            new_key = st.text_input(tr("Add Pixabay API Key"), key="pixabay_new_key")
            if st.button(tr("Add Pixabay API Key")):
                if new_key and new_key not in config.app["pixabay_api_keys"]:
                    config.app["pixabay_api_keys"].append(new_key)
                    config.save_config()
                    st.success(tr("Pixabay API Key added successfully"))
                elif new_key in config.app["pixabay_api_keys"]:
                    st.warning(tr("This API Key already exists"))
                else:
                    st.error(tr("Please enter a valid API Key"))

            if config.app["pixabay_api_keys"]:
                delete_key = st.selectbox(
                    tr("Select Pixabay API Key to delete"), config.app["pixabay_api_keys"], key="pixabay_delete_key"
                )
                if st.button(tr("Delete Selected Pixabay API Key")):
                    config.app["pixabay_api_keys"].remove(delete_key)
                    config.save_config()
                    st.success(tr("Pixabay API Key deleted successfully"))

def _validate_generation_params():
    """Shared pre-generation validations. Calls st.stop() when invalid."""
    if not params.video_subject and not params.video_script:
        st.error(tr("Video Script and Subject Cannot Both Be Empty"))
        scroll_to_bottom()
        st.stop()

    if params.video_source not in ["pexels", "pixabay", "local", "llm", "llm-video"]:
        st.error(tr("Please Select a Valid Video Source"))
        scroll_to_bottom()
        st.stop()

    if params.video_source == "pexels" and not config.app.get("pexels_api_keys", ""):
        st.error(tr("Please Enter the Pexels API Key"))
        scroll_to_bottom()
        st.stop()

    if params.video_source == "pixabay" and not config.app.get("pixabay_api_keys", ""):
        st.error(tr("Please Enter the Pixabay API Key"))
        scroll_to_bottom()
        st.stop()


# 強制走故事版流程：隱藏一鍵生成影片按鈕，故事版為唯一入口
start_button = False
storyboard_button = st.button(
    tr("Create Storyboard"), use_container_width=True, type="primary"
)

if start_button:
    config.save_config()
    task_id = str(uuid4())
    _validate_generation_params()

    if uploaded_files:
        local_videos_dir = utils.storage_dir("local_videos", create=True)
        for file in uploaded_files:
            file_path = os.path.join(local_videos_dir, f"{file.file_id}_{file.name}")
            with open(file_path, "wb") as f:
                f.write(file.getbuffer())
                m = MaterialInfo()
                m.provider = "local"
                m.url = file_path
                if not params.video_materials:
                    params.video_materials = []
                params.video_materials.append(m)

    log_container = st.empty()
    log_records = []

    def log_received(msg):
        if config.ui["hide_log"]:
            return
        with log_container:
            log_records.append(msg)
            st.code("\n".join(log_records))

    logger.add(log_received)

    st.toast(tr("Generating Video"))
    logger.info(tr("Start Generating Video"))
    logger.info(utils.to_json(params))
    scroll_to_bottom()

    result = tm.start(task_id=task_id, params=params)
    if not result or "videos" not in result:
        st.error(tr("Video Generation Failed"))
        logger.error(tr("Video Generation Failed"))
        scroll_to_bottom()
        st.stop()

    video_files = result.get("videos", [])
    st.success(tr("Video Generation Completed"))
    try:
        if video_files:
            player_cols = st.columns(len(video_files) * 2 + 1)
            for i, url in enumerate(video_files):
                player_cols[i * 2 + 1].video(url)
    except Exception:
        pass

    open_task_folder(task_id)
    logger.info(tr("Video Generation Completed"))
    scroll_to_bottom()


# ── 故事版流程：先產生腳本/配音/字幕/素材，逐段檢視後再合成 ─────────────────
def _split_script_chunks(script_text: str, n: int):
    """Split the script into n roughly-even chunks. Falls back from sentence
    enders to commas, then to plain character slicing, so scripts written as
    one long sentence still spread across all segments."""
    if n <= 0:
        return []
    text = (script_text or "").strip()
    if not text:
        return [""] * n
    parts = [s for s in re.split(r"(?<=[。！？.!?])\s*", text) if s.strip()]
    if len(parts) < n:
        finer = []
        for p in (parts or [text]):
            finer.extend(x for x in re.split(r"(?<=[，,、；;：:])\s*", p) if x.strip())
        if len(finer) >= len(parts):
            parts = finer
    if len(parts) < n:
        size = max(1, len(text) // n)
        parts = [text[i * size:(i + 1) * size] for i in range(n - 1)]
        parts.append(text[(n - 1) * size:])
        parts = [p for p in parts if p]
    # distribute parts into n buckets balanced by cumulative length
    chunks = [""] * n
    total = sum(len(p) for p in parts)
    acc = 0
    for p in parts:
        idx = min(n - 1, int(acc * n / max(1, total)))
        chunks[idx] += p
        acc += len(p)
    return chunks


def _storyboard_file(task_id: str) -> str:
    return os.path.join(utils.task_dir(task_id), "storyboard.json")


def _load_storyboard_data(task_id: str):
    """Load storyboard.json → (style, segments); tolerates the legacy list shape."""
    try:
        with open(_storyboard_file(task_id), "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return "", []
    if isinstance(data, list):
        data = {"style": "", "segments": data}
    segments = data.get("segments", [])
    for s in segments:
        if not s.get("uid"):
            s["uid"] = uuid4().hex[:8]
    return data.get("style", ""), segments


def _load_characters(task_id: str) -> list:
    try:
        with open(_storyboard_file(task_id), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("characters", []) if isinstance(data, dict) else []
    except Exception:
        return []


def _load_stage(task_id: str) -> str:
    """The persisted editing stage ('board' | 'segments'), so reopening resumes
    exactly where the user left off. Empty string if none saved."""
    try:
        with open(_storyboard_file(task_id), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("stage", "") if isinstance(data, dict) else ""
    except Exception:
        return ""


def _save_storyboard_data(task_id: str, style: str, segments: list, characters=None, stage=None):
    # characters/stage = None → keep whatever is already on disk
    existing = {}
    if characters is None or stage is None:
        try:
            with open(_storyboard_file(task_id), "r", encoding="utf-8") as f:
                existing = json.load(f)
                if not isinstance(existing, dict):
                    existing = {}
        except Exception:
            existing = {}
    if characters is None:
        characters = existing.get("characters", [])
    if stage is None:
        stage = existing.get("stage", "board")
    with open(_storyboard_file(task_id), "w", encoding="utf-8") as f:
        json.dump({"style": style, "characters": characters, "segments": segments,
                   "stage": stage}, f, ensure_ascii=False, indent=2)


def _ensure_character_refs(task_id, characters, style, aspect):
    """Lazily give each character a fixed appearance + a reference portrait so
    their look stays consistent across segment images. Mutates & persists
    characters (adds 'appearance' and 'ref_image'). Returns True if changed."""
    if not characters:
        return False
    changed = False
    # 1. fill in missing appearances in one LLM call
    missing = [c for c in characters if not (c.get("appearance") or "").strip()]
    if missing:
        appmap = llm.generate_character_appearances(
            characters, style=style,
            language=st.session_state.get("ui_language", ""))
        for c in characters:
            if not (c.get("appearance") or "").strip() and c.get("name") in appmap:
                c["appearance"] = appmap[c["name"]]
                changed = True
    # 2. generate a reference portrait per character if missing
    for c in characters:
        rp = c.get("ref_image", "")
        if rp and os.path.exists(rp):
            continue
        if not (c.get("appearance") or "").strip():
            continue
        img = material.generate_character_reference(
            task_id, c.get("name", ""), c.get("appearance", ""), aspect, style=style)
        if img:
            c["ref_image"] = img
            changed = True
    return changed


def _segment_character_refs(seg, characters):
    """Return (ref_image_paths, combined_appearance) for the characters that
    appear in this segment's dialogue (fallback: all characters)."""
    if not characters:
        return [], ""
    names = set()
    for ln in voice.parse_dialogue_lines(seg.get("dialogue_text", "") or ""):
        if ln.get("speaker"):
            names.add(ln["speaker"].strip())
    present = [c for c in characters if c.get("name") in names] or characters
    refs = [c["ref_image"] for c in present if c.get("ref_image") and os.path.exists(c["ref_image"])]
    appearance = "；".join(f"{c.get('name','')}：{c.get('appearance','')}"
                          for c in present if c.get("appearance"))
    return refs, appearance


def _build_storyboard(task_id: str, materials: list, segment_count: int = 0, drama: dict = None) -> list:
    """Build (and persist) the editable storyboard for a task: per-segment
    script chunk, image prompt, LLM-generated transition note and must-say line."""
    sb_dir = utils.task_dir(task_id)
    script_text, terms = "", []
    try:
        with open(os.path.join(sb_dir, "script.json"), "r", encoding="utf-8") as f:
            _sd = json.loads(f.read())
            script_text = _sd.get("script", "")
            terms = _sd.get("search_terms", []) or []
    except Exception:
        pass
    n = len(materials) or int(segment_count)
    if n <= 0 and script_text:
        # 重開舊任務時不知道原段數 → 依腳本長度估算（與後端相同邏輯）
        ascii_ratio = sum(1 for c in script_text if ord(c) < 128) / max(1, len(script_text))
        est = max(10.0, len(script_text.split()) / 2.5) if ascii_ratio > 0.7 \
            else max(10.0, len(script_text) / 4.0)
        n = max(1, min(8, math.ceil(est / max(1, params.video_clip_duration))))
    n = max(1, n)
    characters = []
    if drama and drama.get("segments"):
        # Character-performance storyboard
        d_segs = drama.get("segments", [])
        n = len(d_segs)
        characters = drama.get("characters", [])
        film_style = drama.get("style", "")
        segments = []
        for i in range(n):
            d = d_segs[i]
            clip = materials[i] if i < len(materials) else ""
            image = ""
            if str(clip).endswith(".png"):
                image = str(clip)
                clip = ""
            segments.append({
                "uid": uuid4().hex[:8],
                "clip": clip, "image": image,
                "prompt": d.get("video_direction", ""),
                "scene": d.get("scene", ""),
                "dialogue_text": d.get("dialogue_text", ""),
                "script_chunk": "",
                "transition_note": d.get("transition_note", ""),
                "must_say": d.get("must_say", ""),
                "video_prompt": d.get("video_direction", ""),
                "transition_effect": d.get("transition_effect", "none"),
            })
        _save_storyboard_data(task_id, film_style, segments, characters=characters)
        return segments

    chunks = _split_script_chunks(script_text, n)
    _nb = llm.generate_storyboard_notes(script_text, chunks)
    notes, film_style = _nb["notes"], _nb["style"]
    segments = []
    for i in range(n):
        clip = materials[i] if i < len(materials) else ""
        image = ""
        if str(clip).endswith(".png.mp4"):
            image = str(clip)[:-4]
        elif str(clip).endswith(".png"):
            image = str(clip)
            clip = ""  # static image only; clip is rendered at segment phase
        term = terms[i % len(terms)] if terms else ""
        segments.append({
            "uid": uuid4().hex[:8],
            "clip": clip,
            "image": image,
            "prompt": term,
            "script_chunk": chunks[i] if i < len(chunks) else "",
            "transition_note": notes[i]["transition_note"] if i < len(notes) else "",
            "must_say": notes[i]["must_say"] if i < len(notes) else "",
            "video_prompt": notes[i].get("video_direction", "") if i < len(notes) else "",
            "transition_effect": notes[i].get("transition_effect", "none") if i < len(notes) else "none",
        })
    _save_storyboard_data(task_id, film_style, segments, characters=[])
    return segments


if storyboard_button:
    config.save_config()
    _validate_generation_params()
    task_id = str(uuid4())
    st.toast(tr("Generating Storyboard"))
    with st.spinner(tr("Generating Storyboard")):
        result = tm.storyboard_start(task_id=task_id, params=params)
    if not result or not (result.get("materials") or result.get("segment_count")):
        st.error(tr("Video Generation Failed"))
        st.stop()
    with st.spinner(tr("Generating Storyboard Notes")):
        _build_storyboard(task_id, result.get("materials", []),
                          segment_count=result.get("segment_count", 0),
                          drama=result.get("drama"))
    st.session_state["storyboard"] = {"task_id": task_id, "stage": "board"}

_sb = st.session_state.get("storyboard")
if _sb:
    st.divider()
    st.subheader("📋 " + tr("Storyboard Review"))
    _sb_tid = _sb["task_id"]
    _sb_dir = utils.task_dir(_sb_tid)

    # 舊任務（從歷史重開）尚無 storyboard.json → 現場建立
    if not os.path.exists(_storyboard_file(_sb_tid)):
        with st.spinner(tr("Generating Storyboard Notes")):
            _build_storyboard(_sb_tid, _sb.get("materials", []))
    _sb_style, _segments = _load_storyboard_data(_sb_tid)
    _sb_chars = _load_characters(_sb_tid)
    _is_drama = getattr(params, "presentation_mode", "narration") == "drama" or bool(_sb_chars)

    _sb_stage = _sb.get("stage", "board")
    if _sb_stage == "auto":
        # Prefer the stage the user explicitly saved; else infer from progress.
        _saved_stage = _load_stage(_sb_tid)
        if _saved_stage in ("board", "segments"):
            _sb_stage = _saved_stage
        else:
            _all_rendered = bool(_segments) and all(
                s.get("segment_video") and os.path.exists(s["segment_video"]) for s in _segments
            )
            _sb_stage = "segments" if _all_rendered else "board"
        st.session_state["storyboard"]["stage"] = _sb_stage

    # 舊版故事版缺全片風格或演繹腳本 → 一次補產
    if _segments and (not _sb_style or all(not s.get("video_prompt") for s in _segments)):
        with st.spinner(tr("Generating Storyboard Notes")):
            _bk = llm.generate_storyboard_notes(
                "".join(s.get("script_chunk", "") for s in _segments),
                [s.get("script_chunk", "") for s in _segments],
            )
        _bk_notes = _bk["notes"]
        if not _sb_style:
            _sb_style = _bk["style"]
        for _bi, s in enumerate(_segments):
            if _bi < len(_bk_notes):
                if not s.get("video_prompt"):
                    s["video_prompt"] = _bk_notes[_bi].get("video_direction", "")
                if not s.get("transition_note"):
                    s["transition_note"] = _bk_notes[_bi].get("transition_note", "")
                if not s.get("must_say"):
                    s["must_say"] = _bk_notes[_bi].get("must_say", "")
                if not s.get("transition_effect"):
                    s["transition_effect"] = _bk_notes[_bi].get("transition_effect", "none")
        _save_storyboard_data(_sb_tid, _sb_style, _segments)

    def _seg_sig(s):
        return hashlib.md5(
            ((s.get("script_chunk") or "") + "|" + (s.get("dialogue_text") or "") + "|"
             + (s.get("clip") or s.get("image") or "")
             ).encode("utf-8")).hexdigest()[:12]

    if _sb_stage == "board":
        _sb_style = st.text_input(
            "🎨 " + tr("Film Style"), value=_sb_style, key=f"sb_{_sb_tid}_style",
            help=tr("Film Style Help"))
        if _is_drama and _sb_chars:
            with st.expander("🎭 " + tr("Cast") + f"（{len(_sb_chars)}）", expanded=True):
                _cast_changed = False
                for _ci, _ch in enumerate(_sb_chars):
                    _cc = st.columns([2, 1, 4])
                    _nn = _cc[0].text_input(tr("Character Name"), value=_ch.get("name", ""),
                                            key=f"cast_{_sb_tid}_{_ci}_name")
                    _gg = _cc[1].selectbox(tr("Gender"), ["female", "male"],
                                           index=0 if _ch.get("gender") == "female" else 1,
                                           format_func=lambda x: tr("Female") if x == "female" else tr("Male"),
                                           key=f"cast_{_sb_tid}_{_ci}_gender")
                    _dd = _cc[2].text_input(tr("Persona"), value=_ch.get("desc", ""),
                                            key=f"cast_{_sb_tid}_{_ci}_desc")
                    if _nn != _ch.get("name") or _gg != _ch.get("gender") or _dd != _ch.get("desc"):
                        _ch["name"], _ch["gender"], _ch["desc"] = _nn, _gg, _dd
                        _cast_changed = True
                _vm_preview = voice.assign_character_voices(_sb_chars)
                st.caption("🔈 " + tr("Voice assignment") + "：" +
                           "　".join(f"{k}→{v.split('-')[-1].replace('Neural','')}" for k, v in _vm_preview.items()))
                if _cast_changed:
                    _save_storyboard_data(_sb_tid, _sb_style, _segments, characters=_sb_chars)
        _total_chars = sum(len((s.get("dialogue_text") if _is_drama else s.get("script_chunk")) or "") for s in _segments)
        _total_est = _total_chars / 4.0
        _target = getattr(params, "video_total_duration", 0) or 0
        _dur_msg = f"⏱ {tr('Estimated total')}：{_total_chars} {tr('chars')} ≈ {_total_est:.0f} {tr('seconds')}"
        if _target:
            _dur_msg += f"　|　{tr('Target')}：{_target} {tr('seconds')}"
            if _total_est > _target * 1.2:
                _dur_msg += f"　⚠️ {tr('Over target hint')}"
        st.caption(_dur_msg)
        for i, seg in enumerate(_segments):
            _uid = seg.get("uid", str(i))
            _hdr_l, _hdr_r = st.columns([5, 1])
            _hdr_l.markdown(f"##### 🎬 {tr('Segment')} {i + 1}")
            if _hdr_r.button("🗑 " + tr("Remove Segment"), key=f"rmseg_{_sb_tid}_{_uid}"):
                _segments.pop(i)
                _save_storyboard_data(_sb_tid, _sb_style, _segments)
                st.rerun()
            c_media, c_text = st.columns([1, 2])
            with c_media:
                _clip = seg.get("clip", "")
                _is_veo = "llm-video-" in os.path.basename(str(_clip))
                if _is_veo and os.path.exists(_clip):
                    st.video(_clip)
                elif seg.get("image") and os.path.exists(seg["image"]):
                    st.image(seg["image"], use_container_width=True)
                elif _clip and os.path.exists(_clip):
                    st.video(_clip)
                else:
                    st.caption("🖼 " + tr("No storyboard image yet"))
                _seg_img = seg.get("image", "")
                _has_img = bool(_seg_img) and os.path.exists(_seg_img)
                if not _has_img:
                    st.caption("ℹ️ " + tr("Generate image first for image-to-video"))
                if st.button("🎥 " + tr("Generate Segment Clip"), key=f"segvid_{_sb_tid}_{_uid}",
                             use_container_width=True):
                    _vp = st.session_state.get(f"sb_{_sb_tid}_{_uid}_vprompt", seg.get("video_prompt", ""))
                    if not (_vp or "").strip():
                        st.error(tr("Video direction required"))
                    else:
                        _rdesc = (seg.get("ref_desc") or "").strip()
                        _vp_full = _vp + (f"。{tr('Also include')}：{_rdesc}" if _rdesc else "")
                        with st.spinner(tr("Generating Segment Clip Wait")):
                            _vid = material.generate_single_video_llm(
                                _sb_tid, _vp_full, params.video_aspect,
                                max_clip_duration=params.video_clip_duration, index=_uid,
                                style=_sb_style,
                                reference_image=_seg_img if _has_img else "",
                            )
                        if _vid:
                            seg["clip"] = _vid
                            seg["video_prompt"] = _vp
                            _save_storyboard_data(_sb_tid, _sb_style, _segments)
                            st.rerun()
                        else:
                            st.error(tr("Video Generation Failed"))
                # 單段產生/重新產生分鏡圖
                _img_btn_label = ("🔄 " + tr("Regenerate Image")) if seg.get("image") \
                    else ("🖼 " + tr("Generate Storyboard Image"))
                if True:
                    if st.button(_img_btn_label, key=f"regen_{_sb_tid}_{_uid}",
                                 use_container_width=True):
                        _new_prompt = st.session_state.get(f"sb_{_sb_tid}_{_uid}_prompt", seg.get("prompt", ""))
                        if not (_new_prompt or "").strip():
                            _new_prompt = seg.get("video_prompt", "") or seg.get("script_chunk", "")
                        _refs, _appear = [], ""
                        if _is_drama and _sb_chars:
                            with st.spinner(tr("Preparing character references")):
                                if _ensure_character_refs(_sb_tid, _sb_chars, _sb_style, params.video_aspect):
                                    _save_storyboard_data(_sb_tid, _sb_style, _segments, characters=_sb_chars)
                            _refs, _appear = _segment_character_refs(seg, _sb_chars)
                        # 加入該段上傳的參考圖與說明
                        _refs = _refs + [p for p in seg.get("ref_uploads", []) if os.path.exists(p)]
                        _rdesc = (seg.get("ref_desc") or "").strip()
                        _img_prompt = _new_prompt + (f"。{tr('Also include')}：{_rdesc}" if _rdesc else "")
                        with st.spinner(tr("Regenerating Image")):
                            _img = material.generate_single_image_llm(
                                _sb_tid, _img_prompt, params.video_aspect, index=_uid,
                                style=_sb_style, reference_images=_refs, appearance=_appear,
                            )
                            if _img:
                                seg["image"] = _img
                                _old_clip = seg.get("clip", "")
                                if _old_clip and os.path.exists(_old_clip):
                                    try:
                                        os.remove(_old_clip)  # stale zoom clip
                                    except OSError:
                                        pass
                                seg["clip"] = ""  # re-rendered at segment phase
                        if _img:
                            seg["prompt"] = _new_prompt
                            _save_storyboard_data(_sb_tid, _sb_style, _segments)
                            st.rerun()
                        else:
                            st.error(tr("Video Generation Failed"))
            with c_text:
                seg["prompt"] = st.text_input(
                    tr("Image Prompt"), value=seg.get("prompt", ""),
                    key=f"sb_{_sb_tid}_{_uid}_prompt")
                if _is_drama:
                    if seg.get("scene"):
                        st.caption("🎬 " + tr("Scene") + "：" + seg.get("scene", ""))
                    seg["dialogue_text"] = st.text_area(
                        tr("Dialogue"), value=seg.get("dialogue_text", ""),
                        key=f"sb_{_sb_tid}_{_uid}_dialogue", height=120,
                        help=tr("Dialogue Help"))
                    seg["script_chunk"] = ""
                else:
                    seg["script_chunk"] = st.text_area(
                        tr("Segment Script"), value=seg.get("script_chunk", ""),
                        key=f"sb_{_sb_tid}_{_uid}_chunk", height=100)
                _ck_len = len((seg["script_chunk"] or "").strip())
                _ck_sec = _ck_len / 4.0
                _rec_chars = params.video_clip_duration * 4
                _ck_msg = (f"{_ck_len} {tr('chars')} ≈ {_ck_sec:.0f} {tr('seconds')}"
                           f"　|　{tr('Clip length suggestion')}：≤ {_rec_chars} {tr('chars')}"
                           f"（{params.video_clip_duration} {tr('seconds')}）")
                if _ck_sec > params.video_clip_duration * 1.5:
                    _ck_msg = "⚠️ " + _ck_msg + "　" + tr("Narration exceeds clip hint")
                st.caption(_ck_msg)
                seg["must_say"] = st.text_input(
                    "🔑 " + tr("Must-say Key Line"), value=seg.get("must_say", ""),
                    key=f"sb_{_sb_tid}_{_uid}_mustsay")
                seg["transition_note"] = st.text_input(
                    "🔗 " + tr("Transition Note"), value=seg.get("transition_note", ""),
                    key=f"sb_{_sb_tid}_{_uid}_trans")
                seg["video_prompt"] = st.text_area(
                    "🎥 " + tr("Video Direction Script"), value=seg.get("video_prompt", ""),
                    key=f"sb_{_sb_tid}_{_uid}_vprompt", height=80)
                _fx_opts = ["none", "fade_in", "fade", "slide_in"]
                _fx_labels = {"none": tr("Hard cut"), "fade_in": tr("Fade in segment"),
                              "fade": tr("Dip to black"), "slide_in": tr("Slide in segment")}
                _cur_fx = seg.get("transition_effect", "none")
                seg["transition_effect"] = st.selectbox(
                    "🎞 " + tr("Segment Transition"), _fx_opts,
                    index=_fx_opts.index(_cur_fx) if _cur_fx in _fx_opts else 0,
                    format_func=lambda x: _fx_labels.get(x, x),
                    key=f"sb_{_sb_tid}_{_uid}_fx")

                # 參考素材：上傳圖片 + 說明，補充特定元素到分鏡圖與素材影片
                with st.expander("🖼 " + tr("Reference materials") +
                                 (f"（{len(seg.get('ref_uploads', []))}）" if seg.get("ref_uploads") else "")):
                    seg["ref_desc"] = st.text_input(
                        tr("Reference description"), value=seg.get("ref_desc", ""),
                        key=f"sb_{_sb_tid}_{_uid}_refdesc", placeholder=tr("Reference desc placeholder"))
                    _ups = st.file_uploader(
                        tr("Upload reference images"), type=["png", "jpg", "jpeg"],
                        accept_multiple_files=True, key=f"sb_{_sb_tid}_{_uid}_refup")
                    if _ups:
                        _saved = seg.get("ref_uploads", [])
                        _existing = {os.path.basename(p) for p in _saved}
                        for _up in _ups:
                            _rn = f"seg-ref-{_uid}-{_up.name}"
                            if _rn in _existing:
                                continue
                            _rp = os.path.join(utils.task_dir(_sb_tid), _rn)
                            with open(_rp, "wb") as _f:
                                _f.write(_up.getbuffer())
                            _saved.append(_rp)
                        if _saved != seg.get("ref_uploads", []):
                            seg["ref_uploads"] = _saved
                            _save_storyboard_data(_sb_tid, _sb_style, _segments)
                            st.rerun()
                    _cur_ups = [p for p in seg.get("ref_uploads", []) if os.path.exists(p)]
                    if _cur_ups:
                        _rcols = st.columns(min(len(_cur_ups), 4))
                        for _ri, _rp in enumerate(_cur_ups[:4]):
                            with _rcols[_ri]:
                                st.image(_rp, use_container_width=True)
                                if st.button("🗑", key=f"rmref_{_sb_tid}_{_uid}_{_ri}"):
                                    try:
                                        os.remove(_rp)
                                    except OSError:
                                        pass
                                    seg["ref_uploads"] = [p for p in seg.get("ref_uploads", []) if p != _rp]
                                    _save_storyboard_data(_sb_tid, _sb_style, _segments)
                                    st.rerun()

        if st.button("➕ " + tr("Add Segment"), key=f"addseg_{_sb_tid}", use_container_width=True):
            _segments.append({
                "uid": uuid4().hex[:8], "clip": "", "image": "", "prompt": "",
                "script_chunk": "", "transition_note": "", "must_say": "",
                "video_prompt": "", "transition_effect": "none",
            })
            _save_storyboard_data(_sb_tid, _sb_style, _segments)
            st.rerun()

        # 每次互動後持久化編輯內容
        _save_storyboard_data(_sb_tid, _sb_style, _segments)

        st.info(tr("Storyboard Hint"))

        c_ok, c_save, c_discard = st.columns(3)
        if c_save.button("💾 " + tr("Save & continue later"), use_container_width=True):
            _save_storyboard_data(_sb_tid, _sb_style, _segments, stage="board")
            st.session_state.pop("storyboard", None)
            st.toast("💾 " + tr("Progress saved"))
            st.rerun()
        if c_ok.button("🎬 " + tr("Render Segments"), use_container_width=True, type="primary"):
            _appended = []
            for i, seg in enumerate(_segments):
                _chunk = (seg.get("script_chunk") or "").strip()
                _ms = (seg.get("must_say") or "").strip()
                if _ms and _ms not in _chunk:
                    seg["script_chunk"] = (_ms + "。" + _chunk) if _chunk else _ms
                    _appended.append(i + 1)
            if _appended:
                st.warning(tr("Must-say merged hint") + "：" + ", ".join(map(str, _appended)))
            _seg_inputs, _reused = [], 0
            for i, s in enumerate(_segments):
                _sv = s.get("segment_video", "")
                if _sv and os.path.exists(_sv) and s.get("rendered_sig") == _seg_sig(s):
                    _reused += 1
                    continue  # 未修改且已渲染 → 直接沿用
                _seg_inputs.append({"clip": s.get("clip"), "image": s.get("image"),
                                    "script_chunk": s.get("script_chunk"),
                                    "dialogue_text": s.get("dialogue_text", ""), "index": i})
            if _reused:
                st.toast(f"♻️ {_reused} / {len(_segments)} " + tr("segments reused"))
            _outs_by_idx = {}
            if _seg_inputs:
                with st.spinner(tr("Rendering Segments")):
                    _outs = tm.generate_segments(_sb_tid, params, _seg_inputs,
                                                 voice_map=voice.assign_character_voices(_sb_chars))
                _outs_by_idx = {inp["index"]: _outs[j] if j < len(_outs) else ""
                                for j, inp in enumerate(_seg_inputs)}
            for i, seg in enumerate(_segments):
                if i in _outs_by_idx:
                    seg["segment_video"] = _outs_by_idx[i]
                    if _outs_by_idx[i]:
                        seg["rendered_sig"] = _seg_sig(seg)
            _save_storyboard_data(_sb_tid, _sb_style, _segments)
            _ok_count = sum(1 for s in _segments
                            if s.get("segment_video") and os.path.exists(s["segment_video"]))
            if not _ok_count:
                st.error(tr("Video Generation Failed"))
            else:
                st.session_state["storyboard"]["stage"] = "segments"
                _save_storyboard_data(_sb_tid, _sb_style, _segments, stage="segments")
                st.rerun()
        if c_discard.button("🗑 " + tr("Discard Storyboard"), use_container_width=True):
            del st.session_state["storyboard"]
            st.rerun()

    elif _sb_stage == "segments":
        st.caption(tr("Segments Review Hint"))
        for i, seg in enumerate(_segments):
            st.markdown(f"##### 🎞️ {tr('Segment')} {i + 1}")
            c_v, c_i = st.columns([1, 2])
            with c_v:
                _sv = seg.get("segment_video", "")
                if _sv and os.path.exists(_sv):
                    st.video(_sv)
                else:
                    st.warning(tr("Video Generation Failed"))
            with c_i:
                if seg.get("must_say"):
                    st.markdown("🔑 **" + tr("Must-say Key Line") + "**：" + seg["must_say"])
                if seg.get("transition_note"):
                    st.markdown("🔗 **" + tr("Transition Note") + "**：" + seg["transition_note"]
                                + f"　`{seg.get('transition_effect', 'none')}`")
                st.caption(seg.get("dialogue_text") or seg.get("script_chunk", ""))
                if st.button("🔁 " + tr("Re-render Segment"), key=f"reseg_{_sb_tid}_{seg.get('uid', i)}",
                             use_container_width=True):
                    with st.spinner(tr("Rendering Segments")):
                        _outs = tm.generate_segments(_sb_tid, params,
                            voice_map=voice.assign_character_voices(_sb_chars), segments=[
                            {"clip": seg.get("clip"), "image": seg.get("image"),
                             "script_chunk": seg.get("script_chunk"),
                             "dialogue_text": seg.get("dialogue_text", ""), "index": i}])
                    if _outs and _outs[0]:
                        seg["segment_video"] = _outs[0]
                        seg["rendered_sig"] = _seg_sig(seg)
                        _save_storyboard_data(_sb_tid, _sb_style, _segments)
                        st.rerun()
                    else:
                        st.error(tr("Video Generation Failed"))

        c_merge, c_back, c_save2, c_drop = st.columns(4)
        if c_save2.button("💾 " + tr("Save & continue later"), use_container_width=True):
            _save_storyboard_data(_sb_tid, _sb_style, _segments, stage="segments")
            st.session_state.pop("storyboard", None)
            st.toast("💾 " + tr("Progress saved"))
            st.rerun()
        if c_merge.button("✅ " + tr("Confirm & Merge"), use_container_width=True, type="primary"):
            _seg_files = [s.get("segment_video") for s in _segments]
            _seg_fx = [s.get("transition_effect", "none") for s in _segments]
            with st.spinner(tr("Merging Segments")):
                _fin = tm.merge_segments(_sb_tid, params, _seg_files, transitions=_seg_fx)
            if not _fin or not _fin.get("videos"):
                st.error(tr("Video Generation Failed"))
            else:
                st.success(tr("Video Generation Completed"))
                for v in _fin["videos"]:
                    st.video(v)
                del st.session_state["storyboard"]
        if c_back.button("⬅ " + tr("Back to Storyboard"), use_container_width=True):
            st.session_state["storyboard"]["stage"] = "board"
            _save_storyboard_data(_sb_tid, _sb_style, _segments, stage="board")
            st.rerun()
        if c_drop.button("🗑 " + tr("Discard Storyboard"), use_container_width=True):
            del st.session_state["storyboard"]
            st.rerun()


# ── 產製歷史區 ─────────────────────────────────────────────────────────────────
st.divider()
with st.expander("📁 " + tr("Generation History"), expanded=True):
    _tasks_root = utils.storage_dir("tasks")
    _entries = []
    if os.path.isdir(_tasks_root):
        for _tid in os.listdir(_tasks_root):
            _tdir = os.path.join(_tasks_root, _tid)
            if os.path.isdir(_tdir):
                _entries.append((os.path.getmtime(_tdir), _tid, _tdir))
    _entries.sort(reverse=True)

    if not _entries:
        st.caption(tr("No history yet"))
    for _mtime, _tid, _tdir in _entries[:12]:
        import datetime as _dt

        _subject, _script_excerpt, _h_script, _h_terms = "", "", "", []
        try:
            with open(os.path.join(_tdir, "script.json"), "r", encoding="utf-8") as f:
                _hd = json.loads(f.read())
                _subject = (_hd.get("params", {}) or {}).get("video_subject", "")
                _h_script = _hd.get("script", "") or ""
                _h_terms = _hd.get("search_terms", []) or []
                _script_excerpt = _h_script[:80]
        except Exception:
            pass
        _finals = sorted(
            f for f in os.listdir(_tdir) if f.startswith("final-") and f.endswith(".mp4")
        )
        _mat_files = sorted(
            f for f in os.listdir(_tdir)
            if f.startswith(("llm-image-", "llm-video-")) and not f.endswith(".png.mp4")
        )
        _when = _dt.datetime.fromtimestamp(_mtime).strftime("%Y-%m-%d %H:%M")
        _title = _subject or _script_excerpt or _tid[:8]
        st.markdown(f"**🎞️ {_title}**　`{_when}`" + ("" if _finals else f"　*({tr('Incomplete')})*"))
        if _script_excerpt:
            st.caption(_script_excerpt + ("…" if len(_script_excerpt) >= 80 else ""))
        if _finals:
            _fcols = st.columns(min(len(_finals), 2) + 1)
            for _i, _f in enumerate(_finals[:2]):
                _fcols[_i].video(os.path.join(_tdir, _f))
        if _mat_files:
            _mcols = st.columns(min(len(_mat_files), 5))
            for _i, _mf in enumerate(_mat_files[:5]):
                _mp = os.path.join(_tdir, _mf)
                with _mcols[_i]:
                    if _mf.endswith(".png"):
                        st.image(_mp, use_container_width=True)
                    elif _mf.endswith(".mp4"):
                        st.video(_mp)
        _seg_vids = sorted(
            (f for f in os.listdir(_tdir) if re.match(r"segment-\d+\.mp4$", f)),
            key=lambda x: int(re.search(r"\d+", x).group()),
        )
        if _seg_vids:
            st.caption("🎞️ " + tr("Segment Videos") + f"（{len(_seg_vids)}）")
            _scols = st.columns(min(len(_seg_vids), 4))
            for _i, _sf in enumerate(_seg_vids[:4]):
                _scols[_i].video(os.path.join(_tdir, _sf))
            if len(_seg_vids) > 4:
                st.caption(tr("Open storyboard to view all segments"))
        _hb_load, _hb_sb, _hb_del = st.columns(3)
        if _hb_load.button("↩️ " + tr("Load & Continue"), key=f"load_task_{_tid}",
                           use_container_width=True):
            st.session_state["_pending_load"] = {
                "subject": _subject, "script": _h_script, "terms": _h_terms,
            }
            st.rerun()

        _h_clips = sorted(
            os.path.join(_tdir, f) for f in os.listdir(_tdir)
            if f.endswith(".png.mp4") or (f.startswith("llm-video-") and f.endswith(".mp4"))
        )
        # Reopen whenever a storyboard exists (even text-only / image-only,
        # e.g. saved at board stage), so editing can always be continued.
        _has_sb = os.path.exists(os.path.join(_tdir, "storyboard.json"))
        if _has_sb or _h_clips:
            if _hb_sb.button("📋 " + tr("Reopen Storyboard"), key=f"sb_task_{_tid}",
                             use_container_width=True):
                st.session_state["_pending_load"] = {
                    "subject": _subject, "script": _h_script, "terms": _h_terms,
                }
                st.session_state["storyboard"] = {"task_id": _tid, "materials": _h_clips,
                                                  "stage": "auto"}
                st.rerun()

        if _hb_del.button("🗑 " + tr("Delete Task"), key=f"del_task_{_tid}",
                          use_container_width=True):
            import shutil as _shutil

            _shutil.rmtree(_tdir, ignore_errors=True)
            st.session_state.pop("storyboard", None)
            st.rerun()
        st.divider()

config.save_config()
