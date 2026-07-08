import json
import logging
import re
import requests
from typing import List

import g4f
from loguru import logger
from openai import AzureOpenAI, OpenAI
from openai.types.chat import ChatCompletion

from app.config import config

_max_retries = 5


def _generate_response(prompt: str) -> str:
    try:
        content = ""
        llm_provider = config.app.get("llm_provider", "openai")
        logger.info(f"llm provider: {llm_provider}")
        if llm_provider == "g4f":
            model_name = config.app.get("g4f_model_name", "")
            if not model_name:
                model_name = "gpt-3.5-turbo-16k-0613"
            content = g4f.ChatCompletion.create(
                model=model_name,
                messages=[{"role": "user", "content": prompt}],
            )
        else:
            api_version = ""  # for azure
            if llm_provider == "moonshot":
                api_key = config.app.get("moonshot_api_key")
                model_name = config.app.get("moonshot_model_name")
                base_url = "https://api.moonshot.cn/v1"
            elif llm_provider == "ollama":
                # api_key = config.app.get("openai_api_key")
                api_key = "ollama"  # any string works but you are required to have one
                model_name = config.app.get("ollama_model_name")
                base_url = config.app.get("ollama_base_url", "")
                if not base_url:
                    base_url = "http://localhost:11434/v1"
            elif llm_provider == "openai":
                api_key = config.app.get("openai_api_key")
                model_name = config.app.get("openai_model_name")
                base_url = config.app.get("openai_base_url", "")
                if not base_url:
                    base_url = "https://api.openai.com/v1"
            elif llm_provider == "oneapi":
                api_key = config.app.get("oneapi_api_key")
                model_name = config.app.get("oneapi_model_name")
                base_url = config.app.get("oneapi_base_url", "")
            elif llm_provider == "azure":
                api_key = config.app.get("azure_api_key")
                model_name = config.app.get("azure_model_name")
                base_url = config.app.get("azure_base_url", "")
                api_version = config.app.get("azure_api_version", "2024-02-15-preview")
            elif llm_provider == "gemini":
                api_key = config.app.get("gemini_api_key")
                model_name = config.app.get("gemini_model_name")
                base_url = config.app.get("gemini_base_url", "")
            elif llm_provider == "qwen":
                api_key = config.app.get("qwen_api_key")
                model_name = config.app.get("qwen_model_name")
                base_url = "***"
            elif llm_provider == "cloudflare":
                api_key = config.app.get("cloudflare_api_key")
                model_name = config.app.get("cloudflare_model_name")
                account_id = config.app.get("cloudflare_account_id")
                base_url = "***"
            elif llm_provider == "deepseek":
                api_key = config.app.get("deepseek_api_key")
                model_name = config.app.get("deepseek_model_name")
                base_url = config.app.get("deepseek_base_url")
                if not base_url:
                    base_url = "https://api.deepseek.com"
            elif llm_provider == "modelscope":
                api_key = config.app.get("modelscope_api_key")
                model_name = config.app.get("modelscope_model_name")
                base_url = config.app.get("modelscope_base_url")
                if not base_url:
                    base_url = "https://api-inference.modelscope.cn/v1/"
            elif llm_provider == "ernie":
                api_key = config.app.get("ernie_api_key")
                secret_key = config.app.get("ernie_secret_key")
                base_url = config.app.get("ernie_base_url")
                model_name = "***"
                if not secret_key:
                    raise ValueError(
                        f"{llm_provider}: secret_key is not set, please set it in the config.toml file."
                    )
            elif llm_provider == "pollinations":
                try:
                    base_url = config.app.get("pollinations_base_url", "")
                    if not base_url:
                        base_url = "https://text.pollinations.ai/openai"
                    model_name = config.app.get("pollinations_model_name", "openai-fast")
                   
                    # Prepare the payload
                    payload = {
                        "model": model_name,
                        "messages": [
                            {"role": "user", "content": prompt}
                        ],
                        "seed": 101  # Optional but helps with reproducibility
                    }
                    
                    # Optional parameters if configured
                    if config.app.get("pollinations_private"):
                        payload["private"] = True
                    if config.app.get("pollinations_referrer"):
                        payload["referrer"] = config.app.get("pollinations_referrer")
                    
                    headers = {
                        "Content-Type": "application/json"
                    }
                    
                    # Make the API request
                    response = requests.post(base_url, headers=headers, json=payload)
                    response.raise_for_status()
                    result = response.json()
                    
                    if result and "choices" in result and len(result["choices"]) > 0:
                        content = result["choices"][0]["message"]["content"]
                        return content.replace("\n", "")
                    else:
                        raise Exception(f"[{llm_provider}] returned an invalid response format")
                        
                except requests.exceptions.RequestException as e:
                    raise Exception(f"[{llm_provider}] request failed: {str(e)}")
                except Exception as e:
                    raise Exception(f"[{llm_provider}] error: {str(e)}")

            if llm_provider not in ["pollinations", "ollama"]:  # Skip validation for providers that don't require API key
                if not api_key:
                    raise ValueError(
                        f"{llm_provider}: api_key is not set, please set it in the config.toml file."
                    )
                if not model_name:
                    raise ValueError(
                        f"{llm_provider}: model_name is not set, please set it in the config.toml file."
                    )
                if not base_url:
                    raise ValueError(
                        f"{llm_provider}: base_url is not set, please set it in the config.toml file."
                    )

            if llm_provider == "qwen":
                import dashscope
                from dashscope.api_entities.dashscope_response import GenerationResponse

                dashscope.api_key = api_key
                response = dashscope.Generation.call(
                    model=model_name, messages=[{"role": "user", "content": prompt}]
                )
                if response:
                    if isinstance(response, GenerationResponse):
                        status_code = response.status_code
                        if status_code != 200:
                            raise Exception(
                                f'[{llm_provider}] returned an error response: "{response}"'
                            )

                        content = response["output"]["text"]
                        return content.replace("\n", "")
                    else:
                        raise Exception(
                            f'[{llm_provider}] returned an invalid response: "{response}"'
                        )
                else:
                    raise Exception(f"[{llm_provider}] returned an empty response")

            if llm_provider == "gemini":
                import google.generativeai as genai

                if not base_url:
                    genai.configure(api_key=api_key, transport="rest")
                else:
                    genai.configure(api_key=api_key, transport="rest", client_options={'api_endpoint': base_url})

                generation_config = {
                    "temperature": 0.5,
                    "top_p": 1,
                    "top_k": 1,
                    "max_output_tokens": 2048,
                }

                safety_settings = [
                    {
                        "category": "HARM_CATEGORY_HARASSMENT",
                        "threshold": "BLOCK_ONLY_HIGH",
                    },
                    {
                        "category": "HARM_CATEGORY_HATE_SPEECH",
                        "threshold": "BLOCK_ONLY_HIGH",
                    },
                    {
                        "category": "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                        "threshold": "BLOCK_ONLY_HIGH",
                    },
                    {
                        "category": "HARM_CATEGORY_DANGEROUS_CONTENT",
                        "threshold": "BLOCK_ONLY_HIGH",
                    },
                ]

                model = genai.GenerativeModel(
                    model_name=model_name,
                    generation_config=generation_config,
                    safety_settings=safety_settings,
                )

                try:
                    response = model.generate_content(prompt)
                    candidates = response.candidates
                    generated_text = candidates[0].content.parts[0].text
                except (AttributeError, IndexError) as e:
                    print("Gemini Error:", e)

                return generated_text

            if llm_provider == "cloudflare":
                response = requests.post(
                    f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{model_name}",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={
                        "messages": [
                            {
                                "role": "system",
                                "content": "You are a friendly assistant",
                            },
                            {"role": "user", "content": prompt},
                        ]
                    },
                )
                result = response.json()
                logger.info(result)
                return result["result"]["response"]

            if llm_provider == "ernie":
                response = requests.post(
                    "https://aip.baidubce.com/oauth/2.0/token", 
                    params={
                        "grant_type": "client_credentials",
                        "client_id": api_key,
                        "client_secret": secret_key,
                    }
                )
                access_token = response.json().get("access_token")
                url = f"{base_url}?access_token={access_token}"

                payload = json.dumps(
                    {
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.5,
                        "top_p": 0.8,
                        "penalty_score": 1,
                        "disable_search": False,
                        "enable_citation": False,
                        "response_format": "text",
                    }
                )
                headers = {"Content-Type": "application/json"}

                response = requests.request(
                    "POST", url, headers=headers, data=payload
                ).json()
                return response.get("result")

            if llm_provider == "azure":
                client = AzureOpenAI(
                    api_key=api_key,
                    api_version=api_version,
                    azure_endpoint=base_url,
                )

            if llm_provider == "modelscope":
                content = ''
                client = OpenAI(
                    api_key=api_key,
                    base_url=base_url,
                )
                response = client.chat.completions.create(
                    model=model_name,
                    messages=[{"role": "user", "content": prompt}],
                    extra_body={"enable_thinking": False},
                    stream=True
                )
                if response:
                    for chunk in response:
                        if not chunk.choices:
                            continue
                        delta = chunk.choices[0].delta
                        if delta and delta.content:
                            content += delta.content
                    
                    if not content.strip():
                        raise ValueError("Empty content in stream response")
                    
                    return content.replace("\n", "")
                else:
                    raise Exception(f"[{llm_provider}] returned an empty response")

            else:
                client = OpenAI(
                    api_key=api_key,
                    base_url=base_url,
                )

            response = client.chat.completions.create(
                model=model_name, messages=[{"role": "user", "content": prompt}]
            )
            if response:
                if isinstance(response, ChatCompletion):
                    content = response.choices[0].message.content
                else:
                    raise Exception(
                        f'[{llm_provider}] returned an invalid response: "{response}", please check your network '
                        f"connection and try again."
                    )
            else:
                raise Exception(
                    f"[{llm_provider}] returned an empty response, please check your network connection and try again."
                )

        return content.replace("\n", "")
    except Exception as e:
        return f"Error: {str(e)}"


def generate_script(
    video_subject: str, language: str = "", paragraph_number: int = 1,
    target_duration: int = 0,
) -> str:
    duration_constraint = ""
    if target_duration and target_duration > 0:
        # ~4 chars/sec for CJK narration, ~2.5 words/sec otherwise
        duration_constraint = (
            f"\n9. the script will be narrated as a {target_duration}-second video. "
            f"Strictly limit the total length so the narration fits: about "
            f"{target_duration * 4} characters if writing in Chinese/Japanese/Korean, "
            f"or about {int(target_duration * 2.5)} words otherwise. Do not exceed this."
        )
    prompt = f"""
# Role: Video Script Generator

## Goals:
Generate a script for a video, depending on the subject of the video.

## Constrains:
1. the script is to be returned as a string with the specified number of paragraphs.
2. do not under any circumstance reference this prompt in your response.
3. get straight to the point, don't start with unnecessary things like, "welcome to this video".
4. you must not include any type of markdown or formatting in the script, never use a title.
5. only return the raw content of the script.
6. do not include "voiceover", "narrator" or similar indicators of what should be spoken at the beginning of each paragraph or line.
7. you must not mention the prompt, or anything about the script itself. also, never talk about the amount of paragraphs or lines. just write the script.
8. respond in the same language as the video subject.{duration_constraint}

# Initialization:
- video subject: {video_subject}
- number of paragraphs: {paragraph_number}
""".strip()
    if language:
        prompt += f"\n- language: {language}"

    final_script = ""
    logger.info(f"subject: {video_subject}")

    def format_response(response):
        # Clean the script
        # Remove asterisks, hashes
        response = response.replace("*", "")
        response = response.replace("#", "")

        # Remove markdown syntax
        response = re.sub(r"\[.*\]", "", response)
        response = re.sub(r"\(.*\)", "", response)

        # Split the script into paragraphs
        paragraphs = response.split("\n\n")

        # Select the specified number of paragraphs
        # selected_paragraphs = paragraphs[:paragraph_number]

        # Join the selected paragraphs into a single string
        return "\n\n".join(paragraphs)

    for i in range(_max_retries):
        try:
            response = _generate_response(prompt=prompt)
            if response:
                final_script = format_response(response)
            else:
                logging.error("gpt returned an empty response")

            # g4f may return an error message
            if final_script and "当日额度已消耗完" in final_script:
                raise ValueError(final_script)

            if final_script:
                break
        except Exception as e:
            logger.error(f"failed to generate script: {e}")

        if i < _max_retries:
            logger.warning(f"failed to generate video script, trying again... {i + 1}")
    if "Error: " in final_script:
        logger.error(f"failed to generate video script: {final_script}")
    else:
        logger.success(f"completed: \n{final_script}")
    return final_script.strip()


def generate_terms(video_subject: str, video_script: str, amount: int = 5) -> List[str]:
    prompt = f"""
# Role: Video Search Terms Generator

## Goals:
Generate {amount} search terms for stock videos, depending on the subject of a video.

## Constrains:
1. the search terms are to be returned as a json-array of strings.
2. each search term should consist of 1-3 words, always add the main subject of the video.
3. you must only return the json-array of strings. you must not return anything else. you must not return the script.
4. the search terms must be related to the subject of the video.
5. reply with english search terms only.

## Output Example:
["search term 1", "search term 2", "search term 3","search term 4","search term 5"]

## Context:
### Video Subject
{video_subject}

### Video Script
{video_script}

Please note that you must use English for generating video search terms; Chinese is not accepted.
""".strip()

    logger.info(f"subject: {video_subject}")

    search_terms = []
    response = ""
    for i in range(_max_retries):
        try:
            response = _generate_response(prompt)
            if "Error: " in response:
                logger.error(f"failed to generate video script: {response}")
                return response
            search_terms = json.loads(response)
            if not isinstance(search_terms, list) or not all(
                isinstance(term, str) for term in search_terms
            ):
                logger.error("response is not a list of strings.")
                continue

        except Exception as e:
            logger.warning(f"failed to generate video terms: {str(e)}")
            if response:
                match = re.search(r"\[.*]", response)
                if match:
                    try:
                        search_terms = json.loads(match.group())
                    except Exception as e:
                        logger.warning(f"failed to generate video terms: {str(e)}")
                        pass

        if search_terms and len(search_terms) > 0:
            break
        if i < _max_retries:
            logger.warning(f"failed to generate video terms, trying again... {i + 1}")

    logger.success(f"completed: \n{search_terms}")
    return search_terms


def generate_character_appearances(characters: list, style: str = "", language: str = "") -> dict:
    """Produce a FIXED, detailed physical appearance for each character so their
    look stays consistent across all generated images. Returns {name: appearance}.
    Appearance is concrete and visual (age, build, hair, face, signature outfit,
    colors) — the kind of detail an image model needs to reproduce a person."""
    if not characters:
        return {}
    lines = []
    for c in characters:
        nm = (c.get("name") or "").strip()
        if nm:
            lines.append(f"{nm}｜{c.get('gender','')}｜{c.get('desc','')}")
    if not lines:
        return {}
    lang_hint = f"Write appearances in {language}." if language else \
        "Write appearances in the same language as the character info."
    style_hint = f" Overall film style: {style}." if style else ""
    prompt = f"""
# Role: Character Designer

## Goal:
For each character below, write ONE fixed, detailed VISUAL appearance description that an image generator can reproduce consistently every time (concrete and stable): approximate age, gender, body build, hair (color/length/style), face features, and a signature outfit with specific colors.{style_hint}

## Constraints:
1. Return ONLY a json object mapping character name -> appearance string.
2. Each appearance under 60 characters if Chinese (25 words otherwise); purely physical/visual, no personality, no scene.
3. Keep them distinct so characters are visually distinguishable.
4. {lang_hint}

## Characters (name｜gender｜persona):
{chr(10).join(lines)}

## Output Example:
{{"小明": "25歲男性，中等身材，黑色短髮，圓框眼鏡，白襯衫配深藍色針織背心", "美麗": "30歲女性，長捲髮棕色，鵝蛋臉，米色針織衫配長裙"}}
""".strip()
    for _ in range(_max_retries):
        try:
            response = _generate_response(prompt)
            if "Error: " in response:
                continue
            m = re.search(r"\{.*\}", response, re.DOTALL)
            if not m:
                continue
            data = json.loads(m.group(0))
            if isinstance(data, dict) and data:
                return {str(k): str(v)[:200] for k, v in data.items()}
        except Exception as e:
            logger.warning(f"character appearance generation failed: {e}")
    return {}


def generate_drama_storyboard(video_script: str, n: int, target_duration: int = 0) -> dict:
    """Drama (character performance) storyboard: the outline is only a structural
    reference — output a cast of characters and per-segment plot with emotional
    dialogue lines. Returns {"style", "characters": [{name, gender, desc}],
    "segments": [{scene, dialogue_text, must_say, transition_note,
    video_direction, transition_effect}]}."""
    fallback = {"style": "", "characters": [], "segments": [
        {"scene": "", "dialogue_text": "", "must_say": "", "transition_note": "",
         "video_direction": "", "transition_effect": "none"} for _ in range(n)]}
    if n <= 0:
        return fallback
    dur_hint = ""
    if target_duration:
        per = max(4, target_duration // max(1, n))
        dur_hint = (f"\n6. The whole film is about {target_duration} seconds; each segment's "
                    f"dialogue must be speakable within ~{per} seconds "
                    f"(≤ {per * 4} Chinese characters of dialogue per segment).")
    prompt = f"""
# Role: Short-Drama Screenwriter & Storyboard Designer

## Goals:
Turn the outline below into a CHARACTER-PERFORMED short drama (NOT documentary narration). The outline is only a structural reference. Design:
1. "style": one film-level visual aesthetic (color palette, photography style, lighting mood).
2. "characters": 2-4 recurring characters, each {{"name": short name, "gender": "male"|"female", "desc": one-line persona}}.
3. exactly {n} "segments", each:
   - "scene": one sentence of what happens in this scene (plot beat).
   - "dialogue_text": the acted dialogue, one line per utterance, EXACT format per line: 角色名（情感）：台詞  — emotion from: 平靜, 開心, 激動, 悲傷, 嚴肅, 溫柔, 疑惑, 緊張. 1-3 lines per segment. Dialogue must carry the plot and feel like real spoken drama, NOT read-aloud narration.
   - "must_say": the one key line (quoted from the dialogue) that must be kept.
   - "video_direction": cinematic direction of the scene showing the characters acting (setting, character actions/expressions, camera movement, lighting), matching the style.
   - "transition_effect": one of "none", "fade_in", "fade", "slide_in" — how the film enters this segment.

## Constrains:
1. Return ONLY a json object {{"style": "...", "characters": [...], "segments": [...]}} with exactly {n} segments.
2. Write everything in the same language as the outline; "transition_effect" and "gender" use the English tokens.
3. Characters must stay consistent across segments; use only declared character names in dialogue_text.
4. Keep "style" under 50 characters; "scene" under 40; "video_direction" under 90.
5. No narrator, no voice-over lines, no text overlays in video_direction.{dur_hint}

## Output Example:
{{"style": "霓虹暖橙色調，夜市街頭電影感，淺景深", "characters": [{{"name": "小婷", "gender": "female", "desc": "第一次逛夜市的大學生，好奇心旺盛"}}, {{"name": "阿伯", "gender": "male", "desc": "賣蚵仔煎三十年的老攤主，豪爽健談"}}], "segments": [{{"scene": "小婷第一次踏進熱鬧的夜市", "dialogue_text": "小婷（開心）：哇，這裡也太熱鬧了吧！\n阿伯（豪爽）：妹妹，來呷看覓，阿伯的蚵仔煎全夜市上出名！", "must_say": "阿伯的蚵仔煎全夜市上出名", "video_direction": "夜市入口人潮湧動，小婷睜大眼睛環顧四周，鏡頭跟隨她穿過攤位，暖黃燈泡串光影", "transition_effect": "fade_in"}}]}}

## Outline (structural reference only)
{video_script}
""".strip()
    _valid_fx = {"none", "fade_in", "fade", "slide_in"}
    for _ in range(_max_retries):
        try:
            response = _generate_response(prompt)
            if "Error: " in response:
                continue
            m = re.search(r"\{.*\}", response, re.DOTALL)
            if not m:
                continue
            data = json.loads(m.group(0))
            chars = []
            for c in (data.get("characters") or [])[:4]:
                if isinstance(c, dict) and c.get("name"):
                    chars.append({"name": str(c["name"])[:20],
                                  "gender": "male" if str(c.get("gender", "")).lower().startswith("m") else "female",
                                  "desc": str(c.get("desc", ""))[:80]})
            arr = data.get("segments", [])
            segs = []
            for i in range(n):
                item = arr[i] if i < len(arr) and isinstance(arr[i], dict) else {}
                fx = str(item.get("transition_effect", "none")).strip()
                segs.append({
                    "scene": str(item.get("scene", ""))[:120],
                    "dialogue_text": str(item.get("dialogue_text", ""))[:600],
                    "must_say": str(item.get("must_say", ""))[:120],
                    "transition_note": str(item.get("scene", ""))[:120],
                    "video_direction": str(item.get("video_direction", ""))[:240],
                    "transition_effect": fx if fx in _valid_fx else "none",
                })
            if not chars or not any(s["dialogue_text"] for s in segs):
                continue
            return {"style": str(data.get("style", ""))[:160], "characters": chars, "segments": segs}
        except Exception as e:
            logger.warning(f"drama storyboard generation failed: {e}")
    return fallback


def generate_storyboard_notes(video_script: str, segments: List[str]) -> dict:
    """Generate the film-level aesthetic style plus per-segment storyboard
    annotations (transition note, must-say key sentence, video direction and a
    concrete transition effect). Returns {"style": str, "notes": [dict, ...]};
    falls back to empty values on any failure."""
    n = len(segments)
    fallback = {"style": "", "notes": [
        {"transition_note": "", "must_say": "", "video_direction": "", "transition_effect": "none"}
        for _ in range(n)]}
    if not n:
        return fallback
    seg_lines = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(segments))
    prompt = f"""
# Role: Film Storyboard & Narrative Board Designer

## Goals:
First, define ONE film-level "style": the unified visual aesthetic of the whole film (color palette, photography/art style, lighting mood), so every segment looks coherent.
Then, for each numbered segment of the video script below, produce:
1. "transition_note": one short sentence describing how this segment connects to the NEXT segment (narrative flow). For the last segment, describe how it closes the video.
2. "must_say": the single most important key sentence of this segment that MUST be spoken, quoted or minimally condensed from the segment text itself.
3. "video_direction": a cinematic video direction script for this segment — describe the concrete scene, subject action, camera movement (e.g. slow pan, dolly-in, aerial), lighting and mood, matching the segment content and the film style. This will drive an AI video generator.
4. "transition_effect": how the film should transition INTO this segment, chosen from exactly: "none" (hard cut), "fade_in" (segment fades in), "fade" (dip to black between segments), "slide_in" (new segment slides in). Choose based on the narrative relationship with the previous segment (e.g. "fade" for a time/era jump, "none"/"fade_in" for continuous flow). The first segment should be "fade_in" or "none".

## Constrains:
1. Return ONLY a json object: {{"style": "...", "segments": [ ... ]}} with exactly {n} objects in "segments", in segment order.
2. Each segment object has exactly the keys "transition_note", "must_say", "video_direction" and "transition_effect".
3. Write "style" and all text values in the same language as the script; "transition_effect" must be one of the four English tokens.
4. Keep "transition_note" and "must_say" under 40 characters if the script is Chinese (15 words otherwise); "video_direction" under 80 characters (30 words otherwise); "style" under 50 characters (20 words otherwise).
5. "video_direction" must describe visible scenes only — no text overlays, no narration content.

## Output Example:
{{"style": "溫暖琥珀色調，紀實攝影風格，柔和自然光，淺景深", "segments": [{{"transition_note": "由起源帶入現代發展", "must_say": "咖啡起源於九世紀的衣索比亞", "video_direction": "衣索比亞高原晨霧中，鏡頭緩慢推進至結滿紅色果實的咖啡樹，牧羊人驚訝觀察羊群，暖色調日出光", "transition_effect": "fade_in"}}]}}

## Video Script
{video_script}

## Segments
{seg_lines}
""".strip()
    for _ in range(_max_retries):
        try:
            response = _generate_response(prompt)
            if "Error: " in response:
                continue
            m = re.search(r"\{.*\}", response, re.DOTALL)
            if not m:
                continue
            data = json.loads(m.group(0))
            if isinstance(data, list):  # tolerate old array-shaped replies
                data = {"style": "", "segments": data}
            if not isinstance(data, dict):
                continue
            arr = data.get("segments", [])
            _valid_fx = {"none", "fade_in", "fade", "slide_in"}
            notes = []
            for i in range(n):
                item = arr[i] if i < len(arr) and isinstance(arr[i], dict) else {}
                fx = str(item.get("transition_effect", "none")).strip()
                notes.append({
                    "transition_note": str(item.get("transition_note", ""))[:120],
                    "must_say": str(item.get("must_say", ""))[:120],
                    "video_direction": str(item.get("video_direction", ""))[:240],
                    "transition_effect": fx if fx in _valid_fx else "none",
                })
            return {"style": str(data.get("style", ""))[:160], "notes": notes}
        except Exception as e:
            logger.warning(f"storyboard notes generation failed: {e}")
    return fallback


def _character_brief(characters: list) -> str:
    """One-line cast summary for prompt conditioning."""
    parts = []
    for c in (characters or []):
        nm = c.get("name", "")
        if not nm:
            continue
        desc = c.get("appearance") or c.get("desc") or ""
        parts.append(f"{nm}（{desc}）" if desc else nm)
    return "；".join(parts)


def generate_plot_board(video_script: str, characters: list, style: str = "",
                        n_cells: int = 25, suggestions: str = "") -> dict:
    """25-cell (5x5) plot storyboard. Break the whole story into n_cells sequential
    visual beats, then compose a SINGLE labeled grid image prompt drawing all beats
    with consistent characters. `suggestions` (optional) is user feedback to steer a
    regeneration. Returns {"beats": [str,...], "image_prompt": str}."""
    cast = _character_brief(characters)
    fallback = {"beats": [], "image_prompt": ""}
    sug_hint = ("\n7. Revision request from the director — honor it: " + suggestions.strip()) \
        if (suggestions or "").strip() else ""
    prompt = f"""
# Role: Film Storyboard Artist

## Goals:
Break the story below into EXACTLY {n_cells} sequential visual beats (a 5x5 storyboard grid, read left-to-right, top-to-bottom). Each beat is one concrete camera shot that advances the plot.

## Constrains:
1. Return ONLY a json object: {{"beats": ["...", "..."]}} with EXACTLY {n_cells} strings, in story order.
2. Each beat: one short visual sentence (who is in frame, their action/expression, the setting). Same language as the story. Under 30 characters (Chinese) / 12 words each.
3. Keep the named characters consistent; use only these characters: {cast or "(derive a small recurring cast)"}.
4. Purely visual — no dialogue text, no narration, no on-screen captions.{sug_hint}

## Story
{video_script}
""".strip()
    beats = []
    for _ in range(_max_retries):
        try:
            response = _generate_response(prompt)
            if "Error: " in response:
                continue
            m = re.search(r"\{.*\}", response, re.DOTALL)
            if not m:
                continue
            data = json.loads(m.group(0))
            arr = data.get("beats", []) if isinstance(data, dict) else []
            beats = [str(x)[:60] for x in arr if str(x).strip()][:n_cells]
            if beats:
                break
        except Exception as e:
            logger.warning(f"plot board beats generation failed: {e}")
    if not beats:
        return fallback
    # pad to n_cells so the grid stays 5x5
    while len(beats) < n_cells:
        beats.append("")
    panel_lines = "; ".join(f"Panel {i + 1}: {b}" for i, b in enumerate(beats) if b)
    style_part = f" Overall visual style: {style.strip()}." if (style or "").strip() else ""
    cast_part = f" Keep these characters visually consistent across every panel: {cast}." if cast else ""
    sug_part = f" Director's revision note: {suggestions.strip()}." if (suggestions or "").strip() else ""
    image_prompt = (
        f"A single storyboard sheet: one 5x5 grid of {n_cells} numbered comic panels "
        f"telling a story in sequence, read left-to-right and top-to-bottom. Clean "
        f"hand-drawn black-and-white storyboard sketch style, each panel clearly framed "
        f"and labeled with its panel number (1-{n_cells}). {panel_lines}.{cast_part}{style_part}{sug_part} "
        f"No color, no speech bubbles, no dialogue text besides the panel numbers."
    )
    return {"beats": beats, "image_prompt": image_prompt}


def generate_art_shots(video_script: str, characters: list, style: str = "",
                       n: int = 9) -> list:
    """9 environment / camera-angle art compositions (establishing shots) of the key
    locations of the story. Returns [{"angle": str, "prompt": str}, ...] of length n."""
    cast = _character_brief(characters)
    prompt = f"""
# Role: Cinematography & Art Direction Designer

## Goals:
From the story below, design EXACTLY {n} environment art compositions (establishing shots) that together cover the key locations and camera set-ups of the film. These are unpeopled or lightly-peopled scene paintings that define the world and lighting.

## Constrains:
1. Return ONLY a json object: {{"shots": [{{"angle": "...", "desc": "..."}}, ...]}} with EXACTLY {n} objects.
2. "angle": short label of the camera set-up (e.g. 廣角全景, 低角度仰視, 過肩鏡頭, 俯視大遠景, 特寫). Same language as the story.
3. "desc": one sentence describing the location, composition, lighting and mood for an AI image generator. Under 50 characters (Chinese) / 20 words. Purely visual, no text overlays.
4. Coherent with the film style: {style or "(define a consistent look)"}. Recurring characters (context only): {cast or "(none)"}.

## Story
{video_script}
""".strip()
    for _ in range(_max_retries):
        try:
            response = _generate_response(prompt)
            if "Error: " in response:
                continue
            m = re.search(r"\{.*\}", response, re.DOTALL)
            if not m:
                continue
            data = json.loads(m.group(0))
            arr = data.get("shots", []) if isinstance(data, dict) else []
            shots = []
            for item in arr[:n]:
                if not isinstance(item, dict):
                    continue
                shots.append({"angle": str(item.get("angle", ""))[:24],
                              "prompt": str(item.get("desc", ""))[:160]})
            if shots:
                while len(shots) < n:
                    shots.append({"angle": "", "prompt": ""})
                return shots
        except Exception as e:
            logger.warning(f"art shots generation failed: {e}")
    return [{"angle": "", "prompt": ""} for _ in range(n)]


def generate_text_board(video_script: str, characters: list, style: str = "",
                        n_segments: int = 0, plot_beats: list = None,
                        art_shots: list = None) -> list:
    """Detailed textual storyboard, one row per segment. Combines the plot beats,
    character designs and art shots into per-segment {"scene", "emotion", "dialogue",
    "action"}. dialogue lines use the 角色名（情感）：台詞 format. Returns a list of
    dicts of length n_segments."""
    n = max(1, int(n_segments) or (len(plot_beats or []) or 5))
    cast = _character_brief(characters)
    beats_ctx = "\n".join(f"- {b}" for b in (plot_beats or []) if b)
    shots_ctx = "\n".join(f"- {s.get('angle','')}：{s.get('prompt','')}"
                          for s in (art_shots or []) if s.get("prompt"))
    ref_block = ""
    if beats_ctx:
        ref_block += f"\n## Plot board beats (visual reference)\n{beats_ctx}"
    if shots_ctx:
        ref_block += f"\n## Environment art shots (visual reference)\n{shots_ctx}"
    prompt = f"""
# Role: Short-Drama Storyboard Writer

## Goals:
Using the story and the visual references below, write a DETAILED textual storyboard split into EXACTLY {n} segments. Each segment is a shootable beat with acted dialogue.

## Constrains:
0. The {n} segments must cover the plot board beats IN ORDER: split the beats into {n} contiguous groups (~len(beats)/{n} each) and make segment k faithfully depict its group of beats, keeping characters consistent with the character sheet above.
1. Return ONLY a json object: {{"segments": [{{"scene": "...", "emotion": "...", "dialogue": "...", "action": "..."}}, ...]}} with EXACTLY {n} objects, in story order.
2. "scene": one sentence of what happens / where (plot beat). Under 40 characters.
3. "emotion": the dominant emotional tone of the characters in this segment. Under 20 characters.
4. "dialogue": acted lines, ONE line per utterance, EXACT format per line: 角色名（情感）：台詞 — emotion from 平靜, 開心, 激動, 悲傷, 嚴肅, 溫柔, 疑惑, 緊張. 1-3 lines.
5. "action": camera + character action breakdown (blocking, expression, camera move, lighting). Under 80 characters. Purely visual, no captions.
6. Same language as the story. Use only these characters: {cast or "(a small recurring cast)"}. Keep them consistent with the film style: {style}.{ref_block}

## Story
{video_script}
""".strip()
    fallback = [{"scene": "", "emotion": "", "dialogue": "", "action": ""} for _ in range(n)]
    for _ in range(_max_retries):
        try:
            response = _generate_response(prompt)
            if "Error: " in response:
                continue
            m = re.search(r"\{.*\}", response, re.DOTALL)
            if not m:
                continue
            data = json.loads(m.group(0))
            arr = data.get("segments", []) if isinstance(data, dict) else []
            rows = []
            for i in range(n):
                item = arr[i] if i < len(arr) and isinstance(arr[i], dict) else {}
                rows.append({
                    "scene": str(item.get("scene", ""))[:120],
                    "emotion": str(item.get("emotion", ""))[:40],
                    "dialogue": str(item.get("dialogue", ""))[:600],
                    "action": str(item.get("action", ""))[:240],
                })
            if any(r["dialogue"] or r["scene"] for r in rows):
                return rows
        except Exception as e:
            logger.warning(f"text board generation failed: {e}")
    return fallback


if __name__ == "__main__":
    video_subject = "生命的意义是什么"
    script = generate_script(
        video_subject=video_subject, language="zh-CN", paragraph_number=1
    )
    print("######################")
    print(script)
    search_terms = generate_terms(
        video_subject=video_subject, video_script=script, amount=5
    )
    print("######################")
    print(search_terms)
    
