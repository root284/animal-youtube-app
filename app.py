import os
import json
import re
import sys
import time
import uuid
import base64
from io import BytesIO
import requests as http_requests
from flask import Flask, render_template, request, jsonify, send_from_directory
import anthropic

try:
    from PIL import Image as PILImage
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

try:
    from openai import OpenAI as OpenAIClient
except ImportError:
    OpenAIClient = None


if not os.environ.get("ANTHROPIC_API_KEY"):
    print("\n[ERROR] ANTHROPIC_API_KEY 환경변수가 설정되지 않았습니다.")
    print("실행 방법: ANTHROPIC_API_KEY=sk-ant-... SUPERTONE_API_KEY=... ELEVENLABS_API_KEY=... OPENAI_API_KEY=... python3 app.py\n")
    sys.exit(1)

ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "").strip()
SUPERTONE_API_KEY  = os.environ.get("SUPERTONE_API_KEY",  "").strip()
OPENAI_API_KEY     = os.environ.get("OPENAI_API_KEY",     "").strip()

def _mask(key: str) -> str:
    return key[:6] + "..." + key[-4:] if len(key) > 10 else "(짧은 키)"

_anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
print("\n" + "="*58)
print(f"  ANTHROPIC_API_KEY  : {'✅ ' + _mask(_anthropic_key) if _anthropic_key else '❌ 미설정 → 서버 종료'}")
print(f"  SUPERTONE_API_KEY  : {'✅ ' + _mask(SUPERTONE_API_KEY) if SUPERTONE_API_KEY else '❌ 미설정 (모하나 TTS 불가)'}")
print(f"  ELEVENLABS_API_KEY : {'✅ ' + _mask(ELEVENLABS_API_KEY) if ELEVENLABS_API_KEY else '❌ 미설정 (캐릭터 TTS 불가)'}")
print(f"  OPENAI_API_KEY     : {'✅ ' + _mask(OPENAI_API_KEY) if OPENAI_API_KEY else '❌ 미설정 (이미지 생성 불가)'}")
print("="*58 + "\n")

MOHANA_VOICE_ID = "2tAT7azT5P0LHAQeoJ0m"  # 프론트에서 모하나 요청 식별자로만 사용

SUPERTONE_VOICE_ID = "400c24c9a2718734a5b404"
SUPERTONE_TTS_BASE = "https://supertoneapi.com/v1/text-to-speech"

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
AUDIO_DIR  = os.path.join(BASE_DIR, "audio")
IMAGES_DIR = os.path.join(BASE_DIR, "images")
ASSETS_DIR = os.path.join(BASE_DIR, "assets")
MOHANA_IMAGE = os.path.join(ASSETS_DIR, "mohana_turnaround.png")

for d in (AUDIO_DIR, IMAGES_DIR):
    os.makedirs(d, exist_ok=True)

app = Flask(__name__)
client = anthropic.Anthropic()
MODEL = "claude-sonnet-4-6"

SCRIPT_SYSTEM = (
    "당신은 한국 동물 유튜브 채널 '모하나'의 전문 스크립트 작가입니다. "
    "동물의 행동·번식·생태에 관한 내용은 반드시 정확한 정보만 사용하며, "
    "불확실하거나 검증되지 않은 내용은 절대 포함하지 않습니다. "
    "스크립트에 이모지를 절대 사용하지 않습니다. "
    "대상 시청자는 초등학교 고학년(10~13세)이므로 전문용어·영어 단어 사용을 최소화하고, "
    "꼭 써야 할 때는 바로 뒤에 쉬운 부연설명을 괄호로 추가합니다. "
    "유머·과장·예능적 표현으로 재미를 극대화하며, "
    "훅은 꽁트·충격 사실·유머로 강하게 시작합니다."
)

# 모하나 캐릭터 설명 (프롬프트에서 재사용)
MOHANA_DESC = (
    "Mohana: 2D flat anime girl, yellow short hair, blue eyes, "
    "bear-ear yellow hat, yellow sailor dress with orange ribbon, "
    "white gloves, white stockings. Maintain exact same character design as reference image."
)

ANIME_BASE = (
    "2D flat anime illustration, bold black outlines, cel-shading flat colors, "
    "NOT 3D, NOT kawaii, NOT childish, NOT photorealistic, "
    "bright vivid colors, clean composition, 16:9 aspect ratio"
)

THUMBNAIL_STYLE = (
    "2D anime illustrated characters on a photorealistic background. "
    "Characters (Mohana and main animal): 2D flat anime, bold black outlines, cel-shading. "
    "Background: photorealistic, cinematic lighting. "
    "NOT 3D characters, NOT kawaii. 16:9 aspect ratio, eye-catching, vibrant"
)

HOST_SCENE_STYLE = (
    "2D anime illustrated character on a photorealistic background. "
    "Character (Mohana): 2D flat anime, bold black outlines, cel-shading. "
    "Background: photorealistic, cinematic lighting. "
    "NOT 3D character, NOT kawaii. 16:9 aspect ratio"
)

TARGET_W, TARGET_H = 1280, 720


def resize_to_720p(img_bytes: bytes) -> bytes:
    if not PIL_AVAILABLE or not img_bytes:
        return img_bytes
    try:
        img = PILImage.open(BytesIO(img_bytes)).convert("RGB")
        img = img.resize((TARGET_W, TARGET_H), PILImage.LANCZOS)
        out = BytesIO()
        img.save(out, format="PNG", optimize=True)
        return out.getvalue()
    except Exception:
        return img_bytes


# ── Core helpers ─────────────────────────────────────────────────────────────

def extract_json(text: str):
    if not text:
        return None
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for pattern in [r"(\[[\s\S]*\])", r"(\{[\s\S]*\})"]:
        m = re.search(pattern, text)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                continue
    return None


def _api_call(fn):
    try:
        return fn()
    except anthropic.RateLimitError:
        time.sleep(3)
        return fn()


def run_with_search(prompt: str, system: str = None) -> str:
    messages = [{"role": "user", "content": prompt}]
    kwargs: dict = {
        "model": MODEL,
        "max_tokens": 4096,
        "tools": [{"type": "web_search_20250305", "name": "web_search"}],
        "messages": messages,
    }
    if system:
        kwargs["system"] = system
    for _ in range(10):
        response = _api_call(lambda: client.messages.create(**kwargs))
        text = "".join(b.text for b in response.content if hasattr(b, "text"))
        if response.stop_reason == "end_turn":
            return text
        if response.stop_reason == "tool_use":
            msgs = list(kwargs["messages"])
            msgs.append({"role": "assistant", "content": response.content})
            tool_results = [
                {"type": "tool_result", "tool_use_id": b.id, "content": ""}
                for b in response.content
                if getattr(b, "type", None) == "tool_use"
            ]
            if tool_results:
                msgs.append({"role": "user", "content": tool_results})
            kwargs["messages"] = msgs
        else:
            return text
    return ""


def run_simple(prompt: str, system: str = None, max_tokens: int = 4096) -> str:
    kwargs: dict = {
        "model": MODEL,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        kwargs["system"] = system
    response = _api_call(lambda: client.messages.create(**kwargs))
    return "".join(b.text for b in response.content if hasattr(b, "text"))


# ── Static file routes ────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/audio/<path:filename>")
def serve_audio(filename):
    return send_from_directory(AUDIO_DIR, filename)

@app.route("/images/<path:filename>")
def serve_images(filename):
    return send_from_directory(IMAGES_DIR, filename)

@app.route("/assets/<path:filename>")
def serve_assets(filename):
    return send_from_directory(ASSETS_DIR, filename)


# ── API Routes ────────────────────────────────────────────────────────────────

@app.route("/api/search-items", methods=["POST"])
def api_search_items():
    try:
        body = request.json or {}
        direction = body.get("direction", "").strip()
        exclude_items = body.get("exclude_items", [])

        direction_line = f"\n추가 방향 지시: {direction}" if direction else ""
        exclude_line = ""
        if exclude_items:
            titles = "\n".join(f"- {t}" for t in exclude_items[:20])
            exclude_line = f"\n\n이미 제작된 아이템 (반드시 제외하고 다른 것 추천):\n{titles}"

        system = (
            "한국 동물 유튜브 채널 콘텐츠 기획자. 재밌고 신기한 동물 뉴스를 발굴. "
            "자연재해·환경위기·멸종위기 등 무거운 주제 제외."
        )
        prompt = (
            f"웹 검색으로 최근 1~2개월 내 화제된 동물 뉴스·이야기 중 "
            f"한국 유튜브 콘텐츠로 좋은 아이템 5개 추천.{direction_line}{exclude_line}\n\n"
            "JSON 배열만 반환:\n"
            '[{"title":"제목","description":"한줄 설명 (50자 이내)","reason":"추천 이유 (30자 이내)",'
            '"link":"실제 URL (없으면 빈 문자열)"}]'
        )
        raw = run_with_search(prompt, system)
        items = extract_json(raw)
        if isinstance(items, list) and items:
            return jsonify({"success": True, "items": items})
        return jsonify({"success": False, "error": "결과 파싱 실패", "raw": raw})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/get-item-detail", methods=["POST"])
def api_get_item_detail():
    try:
        item = request.json.get("item", "")
        prompt = (
            f"다음 동물 관련 아이템에 대해 웹 검색으로 구체적인 정보를 찾아줘:\n\n"
            f"아이템: {item}\n\n"
            "찾아야 할 정보:\n"
            "- 검증된 구체적인 사실·수치·날짜\n"
            "- 재밌고 신기한 디테일 (확인된 것만)\n"
            "- 관련된 흥미로운 에피소드나 배경\n"
            "- 전문가 의견이나 연구 결과 (있으면)\n"
            "- 유튜브 콘텐츠에서 강조하면 좋을 포인트\n\n"
            "중요: 불확실하거나 확인되지 않은 정보는 포함하지 말 것.\n"
            "핵심 내용을 500자 이내로 요약해줘 (한국어)."
        )
        detail = run_with_search(prompt)
        return jsonify({"success": True, "detail": detail})
    except Exception as e:
        return jsonify({"success": False, "error": str(e), "detail": ""}), 500


@app.route("/api/generate-script", methods=["POST"])
def api_generate_script():
    try:
        d = request.json
        item_detail = d.get("item_detail", "")
        detail_section = f"\n[웹서치 디테일 (검증된 정보만 반영)]\n{item_detail}\n" if item_detail else ""

        prompt = (
            f"주제: {d.get('item', '')}\n"
            f"{detail_section}\n"
            "유튜브 영상 스크립트를 작성해줘.\n\n"
            "조건: 진행자 모하나, 초등 고학년(10~13세), 700~1000자, 예능 느낌, 웹서치 정보 반영\n\n"
            "[훅] → 꽁트·충격 사실·유머로 강하게 시작\n"
            "[인트로] → 모하나 인사 + 주제 소개\n"
            "[본문] → 수치·에피소드·확인된 사실 활용\n"
            "[동물 인터뷰 (선택)] → [모하나] 대사 / [동물이름] 대사 태그 형식\n"
            "[마무리] → 핵심 한줄 정리 + 구독/좋아요\n\n"
            "스크립트만 반환 (이모지 없이, 인터뷰는 태그 형식):"
        )
        script = run_simple(prompt, system=SCRIPT_SYSTEM)
        return jsonify({"success": True, "script": script})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/revise-script", methods=["POST"])
def api_revise_script():
    try:
        d = request.json
        script = d.get("script", "")
        comment = d.get("comment", "")
        prompt = (
            f"스크립트:\n{script}\n\n"
            f"수정 요청: {comment}\n\n"
            "구조·분량(700~1000자) 유지. 수정된 스크립트만 반환:"
        )
        revised = run_simple(prompt, system=SCRIPT_SYSTEM)
        return jsonify({"success": True, "script": revised})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/fact-check", methods=["POST"])
def api_fact_check():
    try:
        script = request.json.get("script", "")
        prompt = (
            f"다음 동물 유튜브 스크립트에서 구체적 수치, 고유명사, 사실적 주장을 추출하고 "
            f"웹 검색으로 팩트 체크해줘:\n\n"
            f"{script}\n\n"
            "판정 기준:\n"
            "- 검증됨: 신뢰할 수 있는 출처로 확인된 정보\n"
            "- 불확실: 확인됐으나 불분명하거나 논란 여지 있음\n"
            "- 오류 가능성: 틀렸거나 과장됐을 가능성\n\n"
            "스크립트에서 3~7개 핵심 클레임만 선별해서 체크.\n"
            "다른 텍스트 없이 JSON 배열만 반환:\n"
            '[{"claim":"주장 내용 (30자 이내)","status":"✅ 검증됨","detail":"근거 설명 (40자 이내)"}]'
        )
        raw = run_with_search(prompt)
        items = extract_json(raw)
        if isinstance(items, list):
            return jsonify({"success": True, "results": items})
        return jsonify({"success": False, "error": "파싱 실패", "raw": raw[:300]})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/script-chat", methods=["POST"])
def api_script_chat():
    try:
        question = request.json.get("question", "")
        script = request.json.get("script", "")
        prompt = (
            f"동물 유튜브 스크립트:\n{script[:800]}\n\n"
            f"사용자 질문: {question}\n\n"
            "스크립트 내용 또는 관련 동물 정보에 대해 정확하게 답해줘. "
            "필요하면 웹 검색을 활용해. 200자 이내로 간결하게."
        )
        answer = run_with_search(prompt, system=SCRIPT_SYSTEM)
        return jsonify({"success": True, "answer": answer})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


def _el_headers() -> dict:
    return {"xi-api-key": ELEVENLABS_API_KEY}

def _el_parse_error(resp) -> str:
    """ElevenLabs 에러 응답을 사람이 읽기 좋은 문자열로 변환."""
    status = resp.status_code
    try:
        body = resp.json()
        # {"detail": {"message": "..."}} 또는 {"detail": "..."}
        detail = body.get("detail") or body.get("message") or body
        if isinstance(detail, dict):
            msg = detail.get("message") or detail.get("status") or str(detail)
        else:
            msg = str(detail)
    except Exception:
        msg = resp.text[:200] or f"HTTP {status}"
    return f"[HTTP {status}] {msg}"


@app.route("/api/tts-voices", methods=["GET"])
def api_tts_voices():
    if not ELEVENLABS_API_KEY:
        return jsonify({"success": False, "error": "ELEVENLABS_API_KEY 미설정"})
    try:
        resp = http_requests.get(
            "https://api.elevenlabs.io/v1/voices",
            headers=_el_headers(),
            timeout=15,
        )
        if resp.status_code != 200:
            return jsonify({"success": False, "error": _el_parse_error(resp)})
        voices_data = resp.json().get("voices", [])
        voices = []
        for v in voices_data:
            labels = v.get("labels") or {}
            voices.append({
                "voice_id": v["voice_id"],
                "name": v["name"],
                "category": v.get("category", ""),
                "gender": labels.get("gender", ""),
                "description": labels.get("description", ""),
            })
        return jsonify({"success": True, "voices": voices[:40]})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


def parse_tagged_script(text: str) -> list:
    """[모하나]/[동물이름] 태그 스크립트를 화자별 세그먼트로 분리. 태그 없으면 [] 반환."""
    tag_pattern = re.compile(r'^\[([^\]]+)\][ \t]*(.*)', re.DOTALL)
    lines = text.split('\n')

    has_any_tag = any(tag_pattern.match(l.strip()) for l in lines)
    if not has_any_tag:
        return []

    segments: list = []
    cur_type: str | None = None
    cur_label: str | None = None
    cur_lines: list = []

    def flush():
        if cur_lines and cur_type is not None:
            joined = ' '.join(l for l in cur_lines if l)
            if joined:
                segments.append({'speaker_type': cur_type, 'speaker_label': cur_label, 'text': joined})

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        m = tag_pattern.match(line)
        if m:
            flush()
            cur_lines = []
            cur_label = m.group(1).strip()
            content = m.group(2).strip()
            cur_type = 'mohana' if cur_label == '모하나' else 'char'
            if content:
                cur_lines.append(content)
        else:
            if cur_type is None:
                cur_type = 'mohana'
                cur_label = '모하나'
            cur_lines.append(line)

    flush()
    return segments


def _call_elevenlabs_tts(voice_id: str, text: str, speed: float, headers: dict) -> bytes:
    def _do():
        return http_requests.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
            headers=headers,
            json={
                "text": text,
                "model_id": "eleven_multilingual_v2",
                "voice_settings": {"stability": 0.5, "similarity_boost": 0.75, "speed": speed},
            },
            timeout=60,
        )
    resp = _do()
    if resp.status_code == 429:
        time.sleep(3)
        resp = _do()
    if resp.status_code != 200:
        raise RuntimeError(_el_parse_error(resp))
    return resp.content


def _call_supertone_tts(text: str, speed: float) -> bytes:
    """슈퍼톤 API로 모하나 음성 생성. 반환값은 MP3 바이트."""
    if not SUPERTONE_API_KEY:
        raise RuntimeError("SUPERTONE_API_KEY 미설정")
    headers = {
        "x-sup-api-key": SUPERTONE_API_KEY,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }
    url = f"{SUPERTONE_TTS_BASE}/{SUPERTONE_VOICE_ID}"
    payload = {
        "text": text,
        "language": "ko",
        "model": "sona_speech_2",
        "output_format": "mp3",
        "config": {"speed": speed},
    }
    resp = http_requests.post(url, headers=headers, json=payload, timeout=60)
    if resp.status_code == 429:
        time.sleep(3)
        resp = http_requests.post(url, headers=headers, json=payload, timeout=60)
    if resp.status_code != 200:
        try:
            body = resp.json()
            msg = body.get("message") or body.get("error") or str(body)
        except Exception:
            msg = resp.text[:200] or f"HTTP {resp.status_code}"
        raise RuntimeError(f"[Supertone {resp.status_code}] {msg}")
    return resp.content


@app.route("/api/generate-interview-tts", methods=["POST"])
def api_generate_interview_tts():
    """
    [모하나]/[동물] 태그로 분리된 대사를 화자별로 각각 별도 파일로 생성해 목록 반환.
    모하나 대사 → Supertone, 동물 대사 → ElevenLabs.
    """
    if not SUPERTONE_API_KEY:
        return jsonify({"success": False, "error": "SUPERTONE_API_KEY 미설정 (모하나 TTS 불가)"})
    try:
        text = (request.json.get("text") or "").strip()
        char_voice_id = (request.json.get("char_voice_id") or "").strip()
        scene_idx = request.json.get("scene_idx")
        speed = float(request.json.get("speed", 1.0))
        speed = max(0.7, min(1.5, speed))

        if not text:
            return jsonify({"success": False, "error": "텍스트가 비어있습니다"})

        segments = parse_tagged_script(text)
        if not segments:
            return jsonify({"success": False, "error": "태그([모하나]/[동물]) 형식의 대사를 찾을 수 없습니다"})

        el_headers = {**_el_headers(), "Content-Type": "application/json"}
        result_segments = []
        scene_prefix = f"scene_{scene_idx}" if scene_idx is not None else "interview"

        for seg_idx, seg in enumerate(segments):
            seg_text = seg['text'].strip()
            if not seg_text:
                continue

            if seg['speaker_type'] == 'mohana':
                # 모하나 → Supertone
                audio_bytes = _call_supertone_tts(seg_text, speed)
            else:
                # 동물 → ElevenLabs
                if not ELEVENLABS_API_KEY:
                    raise RuntimeError("ELEVENLABS_API_KEY 미설정 (캐릭터 TTS 불가)")
                if not char_voice_id:
                    raise RuntimeError("캐릭터 voice_id가 지정되지 않았습니다")
                audio_bytes = _call_elevenlabs_tts(char_voice_id, seg_text, speed, el_headers)

            filename = f"{scene_prefix}_s{seg_idx}_{uuid.uuid4().hex[:8]}.mp3"
            with open(os.path.join(AUDIO_DIR, filename), "wb") as f:
                f.write(audio_bytes)
            result_segments.append({
                "speaker_type": seg['speaker_type'],
                "speaker_label": seg['speaker_label'],
                "text": seg_text,
                "url": f"/audio/{filename}",
                "filename": filename,
            })

        if not result_segments:
            return jsonify({"success": False, "error": "생성된 음성 세그먼트가 없습니다"})

        return jsonify({"success": True, "segments": result_segments})
    except RuntimeError as e:
        return jsonify({"success": False, "error": str(e)})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/generate-tts", methods=["POST"])
def api_generate_tts():
    """
    단일 음성 생성.
    - voice_id == MOHANA_VOICE_ID  → Supertone (모하나)
    - 그 외 voice_id              → ElevenLabs (동물 캐릭터)
    """
    try:
        text = (request.json.get("text") or "").strip()
        voice_id = (request.json.get("voice_id") or "").strip()
        scene_idx = request.json.get("scene_idx")

        if not text:
            return jsonify({"success": False, "error": "텍스트가 비어있습니다"})
        if not voice_id:
            return jsonify({"success": False, "error": "voice_id가 필요합니다"})

        speed = float(request.json.get("speed", 1.0))
        speed = max(0.7, min(1.5, speed))

        prefix = f"scene_{scene_idx}_" if scene_idx is not None else "tts_"
        filename = f"{prefix}{uuid.uuid4().hex[:8]}.mp3"

        if voice_id == MOHANA_VOICE_ID:
            # 모하나 → Supertone
            if not SUPERTONE_API_KEY:
                return jsonify({"success": False, "error": "SUPERTONE_API_KEY 미설정"})
            audio_bytes = _call_supertone_tts(text, speed)
        else:
            # 동물 캐릭터 → ElevenLabs
            if not ELEVENLABS_API_KEY:
                return jsonify({"success": False, "error": "ELEVENLABS_API_KEY 미설정"})
            el_headers = {**_el_headers(), "Content-Type": "application/json"}
            audio_bytes = _call_elevenlabs_tts(voice_id, text, speed, el_headers)

        with open(os.path.join(AUDIO_DIR, filename), "wb") as f:
            f.write(audio_bytes)
        return jsonify({"success": True, "filename": filename, "url": f"/audio/{filename}"})
    except RuntimeError as e:
        return jsonify({"success": False, "error": str(e)})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500



@app.route("/api/generate-image", methods=["POST"])
def api_generate_image():
    if not OPENAI_API_KEY:
        return jsonify({"success": False, "error": "OPENAI_API_KEY 미설정"})
    if OpenAIClient is None:
        return jsonify({"success": False, "error": "openai 패키지 미설치. pip install openai"})
    try:
        d = request.json
        prompt = (d.get("prompt") or "").strip()
        scene_type = d.get("scene_type", "illustration")  # host | illustration | thumbnail
        scene_idx = d.get("scene_idx")

        if not prompt:
            return jsonify({"success": False, "error": "프롬프트가 비어있습니다"})

        oc = OpenAIClient(api_key=OPENAI_API_KEY)
        img_bytes = None
        use_ref = scene_type in ("host", "thumbnail") and os.path.exists(MOHANA_IMAGE)

        if use_ref:
            with open(MOHANA_IMAGE, "rb") as img_file:
                resp = oc.images.edit(
                    model="gpt-image-1",
                    image=img_file,
                    prompt=prompt,
                    n=1,
                    size="1536x1024",
                    quality="high",
                )
            b64 = resp.data[0].b64_json
            if b64:
                img_bytes = base64.b64decode(b64)
            elif getattr(resp.data[0], "url", None):
                img_bytes = http_requests.get(resp.data[0].url, timeout=30).content
        else:
            resp = oc.images.generate(
                model="gpt-image-1",
                prompt=prompt,
                n=1,
                size="1536x1024",
                quality="high",
            )
            b64 = resp.data[0].b64_json
            if b64:
                img_bytes = base64.b64decode(b64)

        if not img_bytes:
            return jsonify({"success": False, "error": "이미지 데이터를 받지 못했습니다"})

        img_bytes = resize_to_720p(img_bytes)

        prefix = f"scene_{scene_idx}_" if scene_idx is not None else f"{scene_type}_"
        filename = f"{prefix}{uuid.uuid4().hex[:8]}.png"
        filepath = os.path.join(IMAGES_DIR, filename)
        with open(filepath, "wb") as f:
            f.write(img_bytes)

        return jsonify({"success": True, "url": f"/images/{filename}", "filename": filename})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/generate-step3", methods=["POST"])
def api_generate_step3():
    try:
        d = request.json
        item = d.get("item", "")
        script = d.get("script", "")

        prompt = (
            f"아이템: {item}\n스크립트:\n{script}\n\n"
            "아래 4가지를 한 번에 생성해줘.\n\n"
            "1. title: 유튜브 제목 (30자 이내, 예능 느낌, 클릭 유도, 이모지 없이)\n"
            "2. thumbnail_text: 썸네일 문구 (15자 이내, 짧고 강렬)\n"
            "3. thumbnail_prompt: 썸네일 이미지 영어 프롬프트\n"
            f"   - 동물/주제 크게 중앙, {MOHANA_DESC} 구석에 작게(1/4 이하), 놀라는 포즈\n"
            "   - 모하나가 주제 가리지 않게. 볼드 텍스트 오버레이 포함\n"
            f"   - 스타일: {THUMBNAIL_STYLE}\n"
            "   thumbnail_search_query: 참고 사진 검색어 (선택)\n\n"
            "4. storyboard: 콘티 6~8개 장면\n"
            "   타입별 필드:\n"
            f"   - host: character_prompt(영어), 스타일: {HOST_SCENE_STYLE}, {MOHANA_DESC}\n"
            "   - real_footage: footage_query(한국어)\n"
            f"   - illustration: image_prompt(영어), 스타일: {ANIME_BASE}\n"
            "   공통: scene, duration, visual, audio, caption, script_text, type\n\n"
            'JSON만 반환:\n'
            '{"title":"","thumbnail_text":"","thumbnail_type":"illustration","thumbnail_prompt":"",'
            '"thumbnail_search_query":"",'
            '"storyboard":[{"scene":"","duration":"","visual":"","audio":"","caption":"",'
            '"script_text":"","type":"host|real_footage|illustration",'
            '"image_prompt":"","footage_query":"","character_prompt":""}]}'
        )
        raw = run_simple(prompt, max_tokens=8192)
        data = extract_json(raw)
        required = ["title", "thumbnail_text", "storyboard"]
        if data and all(k in data for k in required):
            thumb_text = data.get("thumbnail_text", "")
            thumb_prompt = data.get("thumbnail_prompt", "")
            # thumbnail_text가 prompt에 없으면 앞에 삽입하여 보장
            if thumb_text and thumb_text.lower() not in thumb_prompt.lower():
                thumb_prompt = f'Large bold overlay text "{thumb_text}". ' + thumb_prompt
            return jsonify({
                "success": True,
                "title": data.get("title", ""),
                "thumbnail_text": thumb_text,
                "thumbnail_type": "illustration",
                "thumbnail_prompt": thumb_prompt,
                "thumbnail_search_query": data.get("thumbnail_search_query", ""),
                "storyboard": data.get("storyboard", []),
            })
        return jsonify({"success": False, "error": "파싱 실패", "raw": raw[:500]})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/regen-title", methods=["POST"])
def api_regen_title():
    try:
        d = request.json
        comment = d.get("comment", "")
        comment_line = f"\n수정 요청: {comment}" if comment else ""
        prompt = (
            f"동물 유튜브 아이템: {d.get('item', '')}\n"
            f"스크립트 앞부분: {d.get('script', '')[:400]}\n\n"
            f"유튜브 제목 1개 생성 (30자 이내, 예능 느낌, 클릭 유도, 이모지 없이){comment_line}\n\n"
            'JSON만 반환: {"title": "제목"}'
        )
        raw = run_simple(prompt)
        data = extract_json(raw)
        title = data.get("title", raw.strip()[:60]) if data else raw.strip()[:60]
        return jsonify({"success": True, "title": title})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/regen-thumbnail-text", methods=["POST"])
def api_regen_thumbnail_text():
    try:
        d = request.json
        comment = d.get("comment", "")
        comment_line = f"\n수정 요청: {comment}" if comment else ""
        prompt = (
            f"유튜브 제목: {d.get('title', '')}\n"
            f"아이템: {d.get('item', '')}\n\n"
            f"썸네일 문구 1개 생성 (15자 이내, 짧고 강렬){comment_line}\n\n"
            'JSON만 반환: {"thumbnail_text": "문구"}'
        )
        raw = run_simple(prompt)
        data = extract_json(raw)
        text = data.get("thumbnail_text", raw.strip()[:20]) if data else raw.strip()[:20]
        return jsonify({"success": True, "thumbnail_text": text})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/regen-thumbnail-prompt", methods=["POST"])
def api_regen_thumbnail_prompt():
    try:
        d = request.json
        title = d.get("title", "")
        thumb_text = d.get("thumbnail_text", "")
        item = d.get("item", "")
        comment = d.get("comment", "")
        comment_line = f"\n수정 요청: {comment}" if comment else ""
        prompt = (
            f"유튜브 제목: {title}\n"
            f"썸네일 문구: {thumb_text}\n"
            f"아이템: {item}\n\n"
            f"썸네일 이미지 프롬프트를 영어로 생성해줘.{comment_line}\n\n"
            "조건:\n"
            f"- 메인(크게): 아이템의 동물/주제를 화면 중앙에 크고 생생하게 배치\n"
            f"- 서브(작게): {MOHANA_DESC}\n"
            "  → 화면 한쪽 구석에 작게(1/4 이하) 배치, 놀라거나 반응하는 포즈\n"
            "  → 모하나가 메인 주제를 가리지 않도록 주의\n"
            f"- 썸네일 문구 '{thumb_text}'를 크고 굵은 볼드 텍스트 오버레이로 반드시 포함\n"
            f"- 스타일: {THUMBNAIL_STYLE}\n\n"
            'JSON만 반환: {"thumbnail_prompt": "English prompt",'
            '"thumbnail_search_query": "참고 검색어 (선택, 없으면 빈 문자열)"}'
        )
        raw = run_simple(prompt)
        data = extract_json(raw)
        if data:
            return jsonify({
                "success": True,
                "thumbnail_type": "illustration",
                "thumbnail_prompt": data.get("thumbnail_prompt", ""),
                "thumbnail_search_query": data.get("thumbnail_search_query", ""),
            })
        return jsonify({"success": False, "error": "파싱 실패"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/regen-scene", methods=["POST"])
def api_regen_scene():
    try:
        d = request.json
        scene = d.get("scene", {})
        idx = d.get("scene_idx", 0)
        comment = d.get("comment", "")
        comment_line = f"\n수정 요청: {comment}" if comment else ""

        prompt = (
            f"스크립트 앞부분:\n{d.get('script', '')[:600]}\n\n"
            f"콘티 {idx+1}번 장면 현재 내용:\n"
            f"- 장면명: {scene.get('scene','')}\n"
            f"- 타입: {scene.get('type','')}\n"
            f"- 화면: {scene.get('visual','')}\n"
            f"- 음향: {scene.get('audio','')}\n"
            f"- 자막: {scene.get('caption','')}\n"
            f"- 스크립트: {scene.get('script_text','')}\n"
            f"{comment_line}\n\n"
            "이 장면을 재생성해줘.\n"
            "타입 선택 기준:\n"
            "- 'host': 진행자 모하나 등장 → character_prompt (영어)\n"
            f"  스타일: {HOST_SCENE_STYLE}\n"
            f"  {MOHANA_DESC}\n"
            "- 'real_footage': 실제 동물 영상·뉴스 화면 → footage_query (한국어 검색어)\n"
            "- 'illustration': AI 생성 가능 장면 → image_prompt\n"
            f"  스타일: {ANIME_BASE}, 텍스트 최소화\n"
            "script_text: 이 장면에서 읽는 스크립트 텍스트\n\n"
            'JSON만 반환:\n'
            '{"scene":"장면명","duration":"시간","visual":"화면","audio":"음향","caption":"자막",'
            '"script_text":"스크립트 텍스트","type":"host|real_footage|illustration",'
            '"image_prompt":"","footage_query":"","character_prompt":""}'
        )
        raw = run_simple(prompt)
        data = extract_json(raw)
        if data and "scene" in data:
            return jsonify({"success": True, "scene": data})
        return jsonify({"success": False, "error": "파싱 실패", "raw": raw[:300]})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_ENV") == "development")
