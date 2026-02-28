import os
import asyncio
import edge_tts
import io
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from dotenv import load_dotenv
from pymongo import MongoClient
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
import requests

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'dev-secret-key-change-in-production')

serializer = URLSafeTimedSerializer(app.secret_key)

FRONTEND_URL = os.getenv('FRONTEND_URL', 'http://localhost:8001')
CORS(app, resources={r"/api/*": {"origins": [FRONTEND_URL, "http://localhost:8001", "http://localhost:3000"]}})

MONGO_URI = os.getenv('MONGO_URI', 'mongodb://localhost:27017/')
client = MongoClient(MONGO_URI)
db = client['youtube_summarizer']
users_collection = db['users']

YOUTUBE_API_KEY = os.getenv('YOUTUBE_API_KEY')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
GOOGLE_CLIENT_ID = os.getenv('GOOGLE_CLIENT_ID')


def create_auth_token(user_id):
    return serializer.dumps(user_id)

def get_current_user_id():
    auth_header = request.headers.get('Authorization', '')
    if auth_header.startswith('Bearer '):
        token = auth_header[7:]
        try:
            user_id = serializer.loads(token, max_age=86400 * 30)
            return user_id
        except (BadSignature, SignatureExpired):
            return None
    return None


@app.route('/api/auth/google', methods=['POST'])
def google_login():
    try:
        token = request.json.get('token')
        if not token:
            return jsonify({'error': 'Token required'}), 400

        idinfo = id_token.verify_oauth2_token(
            token, google_requests.Request(), GOOGLE_CLIENT_ID
        )

        user_id = idinfo['sub']
        email = idinfo['email']
        name = idinfo.get('name', '')
        picture = idinfo.get('picture', '')

        user = users_collection.find_one({'user_id': user_id})

        if not user:
            user = {
                'user_id': user_id,
                'email': email,
                'name': name,
                'picture': picture,
                'channels': []
            }
            users_collection.insert_one(user)

        auth_token = create_auth_token(user_id)

        return jsonify({
            'success': True,
            'token': auth_token,
            'user': {
                'user_id': user_id,
                'email': email,
                'name': name,
                'picture': picture,
                'channels': user.get('channels', [])
            }
        })

    except ValueError:
        return jsonify({'error': 'Invalid token'}), 401
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/auth/logout', methods=['POST'])
def logout():
    return jsonify({'success': True})


@app.route('/api/auth/check', methods=['GET'])
def check_auth():
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({'authenticated': False}), 401

    user = users_collection.find_one({'user_id': user_id})
    if not user:
        return jsonify({'authenticated': False}), 401

    return jsonify({
        'authenticated': True,
        'user': {
            'user_id': user['user_id'],
            'email': user['email'],
            'name': user['name'],
            'picture': user.get('picture', ''),
            'channels': user.get('channels', [])
        }
    })


@app.route('/api/channels', methods=['GET'])
def get_channels():
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({'error': 'Unauthorized'}), 401

    user = users_collection.find_one({'user_id': user_id})
    return jsonify({'channels': user.get('channels', []) if user else []})


@app.route('/api/channels', methods=['POST'])
def add_channel():
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({'error': 'Unauthorized'}), 401

    data = request.json
    channel = {
        'url': data.get('url'),
        'id': data.get('id'),
        'name': data.get('name')
    }

    users_collection.update_one(
        {'user_id': user_id},
        {'$push': {'channels': channel}}
    )

    return jsonify({'success': True, 'channel': channel})


@app.route('/api/channels/<int:index>', methods=['DELETE'])
def delete_channel(index):
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({'error': 'Unauthorized'}), 401

    user = users_collection.find_one({'user_id': user_id})

    if user and 'channels' in user:
        channels = user['channels']
        if 0 <= index < len(channels):
            channels.pop(index)
            users_collection.update_one(
                {'user_id': user_id},
                {'$set': {'channels': channels}}
            )
            return jsonify({'success': True})

    return jsonify({'error': 'Channel not found'}), 404


@app.route('/api/keys', methods=['GET'])
def get_api_keys():
    return jsonify({
        'youtube': YOUTUBE_API_KEY,
        'gemini': GEMINI_API_KEY
    })


@app.route('/api/tts', methods=['POST'])
def text_to_speech():
    try:
        data = request.json
        text = data.get('text', '')

        if not text:
            return jsonify({'error': 'Text required'}), 400

        voice = 'ko-KR-SunHiNeural'

        async def generate():
            communicate = edge_tts.Communicate(text, voice)
            audio_data = io.BytesIO()
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    audio_data.write(chunk["data"])
            audio_data.seek(0)
            return audio_data

        audio_data = asyncio.run(generate())

        return send_file(
            audio_data,
            mimetype='audio/mpeg',
            as_attachment=False,
            download_name='speech.mp3'
        )

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/transcript/<video_id>', methods=['GET'])
def get_transcript(video_id):
    import re
    import json as jsonlib
    import time

    debug = request.args.get('debug') == '1'
    logs = []

    # 방법 1: youtube-transcript-api (신버전 1.x)
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        api = YouTubeTranscriptApi()
        fetched = api.fetch(video_id, languages=['ko', 'en'])
        text = ' '.join([item.text if hasattr(item, 'text') else item.get('text', '') for item in fetched])
        if text.strip():
            logs.append('방법1 신API 성공: ' + str(len(text)) + '자')
            if debug:
                return jsonify({'transcript': text[:200], 'debug': logs, 'video_id': video_id})
            return jsonify({'transcript': text, 'video_id': video_id})
        logs.append('방법1 신API: 텍스트 비어있음')
    except Exception as e:
        logs.append('방법1 신API 실패: ' + str(e))

    # 방법 1b: youtube-transcript-api (구버전 0.x)
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        fetched = YouTubeTranscriptApi.get_transcript(video_id, languages=['ko', 'en'])
        text = ' '.join([item.get('text', '') for item in fetched])
        if text.strip():
            logs.append('방법1b 구API 성공: ' + str(len(text)) + '자')
            if debug:
                return jsonify({'transcript': text[:200], 'debug': logs, 'video_id': video_id})
            return jsonify({'transcript': text, 'video_id': video_id})
        logs.append('방법1b 구API: 텍스트 비어있음')
    except Exception as e:
        logs.append('방법1b 구API 실패: ' + str(e))

    # 방법 2: Innertube API (2회 재시도)
    clients = [
        {
            'name': 'IOS',
            'payload': {
                'context': {
                    'client': {
                        'clientName': 'IOS',
                        'clientVersion': '19.45.4',
                        'hl': 'ko',
                        'gl': 'KR',
                        'deviceMake': 'Apple',
                        'deviceModel': 'iPhone16,2',
                        'osName': 'iOS',
                        'osVersion': '18.1.0.22B83'
                    }
                },
                'videoId': video_id
            },
            'key': 'AIzaSyB-63vPrdThhKuerbB2N_l7Kwwcxj6yUAc'
        },
        {
            'name': 'ANDROID',
            'payload': {
                'context': {
                    'client': {
                        'clientName': 'ANDROID',
                        'clientVersion': '19.09.37',
                        'hl': 'ko',
                        'gl': 'KR',
                        'androidSdkVersion': 30
                    }
                },
                'videoId': video_id
            },
            'key': 'AIzaSyA8eiZmM1FaDVjRy-df2KTyQ_vz_yYM39w'
        },
        {
            'name': 'TV_EMBED',
            'payload': {
                'context': {
                    'client': {
                        'clientName': 'TVHTML5_SIMPLY_EMBEDDED_PLAYER',
                        'clientVersion': '2.0',
                        'hl': 'ko',
                        'gl': 'KR'
                    },
                    'thirdParty': {
                        'embedUrl': 'https://www.google.com'
                    }
                },
                'videoId': video_id
            },
            'key': 'AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8'
        },
        {
            'name': 'WEB',
            'payload': {
                'context': {
                    'client': {
                        'clientName': 'WEB',
                        'clientVersion': '2.20250101.00.00',
                        'hl': 'ko',
                        'gl': 'KR'
                    }
                },
                'videoId': video_id
            },
            'key': 'AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8'
        }
    ]

    for c in clients:
        for attempt in range(2):
            try:
                if attempt > 0:
                    time.sleep(1)
                res = requests.post(
                    'https://youtubei.googleapis.com/youtubei/v1/player?key=' + c['key'],
                    json=c['payload'],
                    headers={'Cookie': 'SOCS=CAISNQgDEitib3FfaWRlbnRpdHlmcm9udGVuZHVpc2VydmVyXzIwMjMwODI5LjA3X3AxGgJlbiACGgYIgLC_pwY; CONSENT=PENDING+987'},
                    timeout=10
                )
                if res.status_code != 200:
                    logs.append('방법2 ' + c['name'] + ' 시도' + str(attempt+1) + ': HTTP ' + str(res.status_code))
                    continue
                data = res.json()
                ps = data.get('playabilityStatus', {})
                if ps.get('status') != 'OK':
                    logs.append('방법2 ' + c['name'] + ' 시도' + str(attempt+1) + ': status=' + ps.get('status', 'unknown'))
                    continue
                tracks = (data.get('captions', {})
                          .get('playerCaptionsTracklistRenderer', {})
                          .get('captionTracks', []))
                if not tracks:
                    logs.append('방법2 ' + c['name'] + ' 시도' + str(attempt+1) + ': 자막트랙 없음')
                    continue

                track = None
                for lang in ['ko', 'en']:
                    track = next((t for t in tracks if t.get('languageCode') == lang), None)
                    if track:
                        break
                if not track:
                    track = tracks[0]

                cap_res = requests.get(track['baseUrl'], timeout=10)
                if cap_res.status_code != 200:
                    logs.append('방법2 ' + c['name'] + ' 시도' + str(attempt+1) + ': 자막다운 HTTP ' + str(cap_res.status_code))
                    continue
                texts = extract_texts(cap_res.text)
                if texts:
                    logs.append('방법2 ' + c['name'] + ' 시도' + str(attempt+1) + ' 성공: ' + str(len(' '.join(texts))) + '자')
                    if debug:
                        return jsonify({'transcript': ' '.join(texts)[:200], 'debug': logs, 'video_id': video_id})
                    return jsonify({'transcript': ' '.join(texts), 'video_id': video_id})
                logs.append('방법2 ' + c['name'] + ' 시도' + str(attempt+1) + ': extractTexts 실패')
            except Exception as e:
                logs.append('방법2 ' + c['name'] + ' 시도' + str(attempt+1) + ' 오류: ' + str(e))

    # 방법 3: Invidious/Piped 대안 API (자체 서버 IP 사용)
    alt_apis = [
        {'name': 'Piped', 'url': 'https://pipedapi.kavin.rocks/streams/' + video_id, 'type': 'piped'},
        {'name': 'Invidious1', 'url': 'https://vid.puffyan.us/api/v1/captions/' + video_id, 'type': 'invidious'},
        {'name': 'Invidious2', 'url': 'https://inv.nadeko.net/api/v1/captions/' + video_id, 'type': 'invidious'},
        {'name': 'Invidious3', 'url': 'https://invidious.nerdvpn.de/api/v1/captions/' + video_id, 'type': 'invidious'},
    ]

    for api in alt_apis:
        try:
            api_res = requests.get(api['url'], timeout=10, headers={
                'User-Agent': 'Mozilla/5.0'
            })
            if api_res.status_code != 200:
                logs.append('방법3 ' + api['name'] + ': HTTP ' + str(api_res.status_code))
                continue

            api_data = api_res.json()

            if api['type'] == 'piped':
                subtitles = api_data.get('subtitles', [])
                if not subtitles:
                    logs.append('방법3 ' + api['name'] + ': 자막 없음')
                    continue
                sub = None
                for lang in ['ko', 'en']:
                    sub = next((s for s in subtitles if s.get('code', '').startswith(lang)), None)
                    if sub:
                        break
                if not sub:
                    sub = subtitles[0]
                sub_url = sub.get('url', '')
                if not sub_url:
                    logs.append('방법3 ' + api['name'] + ': URL 없음')
                    continue
                cap_res = requests.get(sub_url, timeout=10)
                if cap_res.status_code == 200 and len(cap_res.text) > 50:
                    texts = extract_texts(cap_res.text)
                    if texts:
                        logs.append('방법3 ' + api['name'] + ' 성공: ' + str(len(' '.join(texts))) + '자')
                        if debug:
                            return jsonify({'transcript': ' '.join(texts)[:200], 'debug': logs, 'video_id': video_id})
                        return jsonify({'transcript': ' '.join(texts), 'video_id': video_id})

            elif api['type'] == 'invidious':
                captions = api_data.get('captions', [])
                if not captions:
                    logs.append('방법3 ' + api['name'] + ': 자막 없음')
                    continue
                cap = None
                for lang in ['ko', 'en']:
                    cap = next((c for c in captions if c.get('language_code', '').startswith(lang)), None)
                    if cap:
                        break
                if not cap:
                    cap = captions[0]
                cap_url = cap.get('url', '')
                if not cap_url:
                    logs.append('방법3 ' + api['name'] + ': URL 없음')
                    continue
                base = api['url'].rsplit('/api/', 1)[0]
                full_url = base + cap_url if cap_url.startswith('/') else cap_url
                cap_res = requests.get(full_url, timeout=10)
                if cap_res.status_code == 200 and len(cap_res.text) > 50:
                    texts = extract_texts(cap_res.text)
                    if texts:
                        logs.append('방법3 ' + api['name'] + ' 성공: ' + str(len(' '.join(texts))) + '자')
                        if debug:
                            return jsonify({'transcript': ' '.join(texts)[:200], 'debug': logs, 'video_id': video_id})
                        return jsonify({'transcript': ' '.join(texts), 'video_id': video_id})

            logs.append('방법3 ' + api['name'] + ': 자막 파싱 실패')
        except Exception as e:
            logs.append('방법3 ' + api['name'] + ' 오류: ' + str(e))

    # 방법 4: timedtext API 직접 호출
    for lang in ['ko', 'en']:
        try:
            tt_url = 'https://www.youtube.com/api/timedtext?v=' + video_id + '&lang=' + lang + '&fmt=srv3'
            tt_res = requests.get(tt_url, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Cookie': 'SOCS=CAISNQgDEitib3FfaWRlbnRpdHlmcm9udGVuZHVpc2VydmVyXzIwMjMwODI5LjA3X3AxGgJlbiACGgYIgLC_pwY; CONSENT=PENDING+987'
            }, timeout=10)
            if tt_res.status_code == 200 and len(tt_res.text) > 100:
                texts = extract_texts(tt_res.text)
                if texts:
                    logs.append('방법4 timedtext ' + lang + ' 성공: ' + str(len(' '.join(texts))) + '자')
                    if debug:
                        return jsonify({'transcript': ' '.join(texts)[:200], 'debug': logs, 'video_id': video_id})
                    return jsonify({'transcript': ' '.join(texts), 'video_id': video_id})
                else:
                    logs.append('방법4 timedtext ' + lang + ': extractTexts 실패, len=' + str(len(tt_res.text)))
            else:
                logs.append('방법4 timedtext ' + lang + ': HTTP ' + str(tt_res.status_code) + ' len=' + str(len(tt_res.text)))
        except Exception as e:
            logs.append('방법4 timedtext ' + lang + ' 오류: ' + str(e))

    # 방법 5: embed 페이지에서 자막 추출
    try:
        embed_res = requests.get(
            'https://www.youtube.com/embed/' + video_id + '?hl=ko',
            headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Cookie': 'SOCS=CAISNQgDEitib3FfaWRlbnRpdHlmcm9udGVuZHVpc2VydmVyXzIwMjMwODI5LjA3X3AxGgJlbiACGgYIgLC_pwY; CONSENT=PENDING+987'
            },
            timeout=15
        )
        if embed_res.status_code == 200:
            match = re.search(r'"captions":\s*(\{.+?"\})', embed_res.text)
            if not match:
                match = re.search(r'ytInitialPlayerResponse\s*=\s*(\{.+?\});', embed_res.text)
            if match:
                player_data = jsonlib.loads(match.group(1))
                tracks = (player_data.get('captions', player_data).get('playerCaptionsTracklistRenderer', {}).get('captionTracks', []))
                if tracks:
                    track = None
                    for lang in ['ko', 'en']:
                        track = next((t for t in tracks if t.get('languageCode') == lang), None)
                        if track:
                            break
                    if not track:
                        track = tracks[0]
                    cap_res = requests.get(track['baseUrl'], timeout=10)
                    if cap_res.status_code == 200:
                        texts = extract_texts(cap_res.text)
                        if texts:
                            logs.append('방법5 embed 성공: ' + str(len(' '.join(texts))) + '자')
                            if debug:
                                return jsonify({'transcript': ' '.join(texts)[:200], 'debug': logs, 'video_id': video_id})
                            return jsonify({'transcript': ' '.join(texts), 'video_id': video_id})
                        else:
                            logs.append('방법5 embed: extractTexts 실패')
                    else:
                        logs.append('방법5 embed: 자막다운 HTTP ' + str(cap_res.status_code))
                else:
                    logs.append('방법5 embed: 자막트랙 없음')
            else:
                logs.append('방법5 embed: 캡션데이터 없음')
        else:
            logs.append('방법5 embed: HTTP ' + str(embed_res.status_code))
    except Exception as e:
        logs.append('방법5 embed 오류: ' + str(e))

    # 방법 6: 웹 스크래핑
    try:
        page_res = requests.get(
            'https://www.youtube.com/watch?v=' + video_id + '&hl=ko&gl=KR',
            headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept-Language': 'ko-KR,ko;q=0.9,en;q=0.8',
                'Cookie': 'SOCS=CAISNQgDEitib3FfaWRlbnRpdHlmcm9udGVuZHVpc2VydmVyXzIwMjMwODI5LjA3X3AxGgJlbiACGgYIgLC_pwY; CONSENT=PENDING+987'
            },
            timeout=15
        )
        if page_res.status_code == 200:
            match = re.search(r'ytInitialPlayerResponse\s*=\s*(\{.+?\});', page_res.text)
            if match:
                player_data = jsonlib.loads(match.group(1))
                tracks = (player_data.get('captions', {})
                          .get('playerCaptionsTracklistRenderer', {})
                          .get('captionTracks', []))
                if tracks:
                    track = None
                    for lang in ['ko', 'en']:
                        track = next((t for t in tracks if t.get('languageCode') == lang), None)
                        if track:
                            break
                    if not track:
                        track = tracks[0]
                    cap_res = requests.get(track['baseUrl'], timeout=10)
                    if cap_res.status_code == 200:
                        texts = extract_texts(cap_res.text)
                        if texts:
                            logs.append('방법6 웹스크래핑 성공: ' + str(len(' '.join(texts))) + '자')
                            if debug:
                                return jsonify({'transcript': ' '.join(texts)[:200], 'debug': logs, 'video_id': video_id})
                            return jsonify({'transcript': ' '.join(texts), 'video_id': video_id})
                        else:
                            logs.append('방법6 웹스크래핑: extractTexts 실패')
                    else:
                        logs.append('방법6 웹스크래핑: 자막다운 HTTP ' + str(cap_res.status_code))
                else:
                    logs.append('방법6 웹스크래핑: 자막트랙 없음')
            else:
                logs.append('방법6 웹스크래핑: ytInitialPlayerResponse 없음')
        else:
            logs.append('방법6 웹스크래핑: 페이지 HTTP ' + str(page_res.status_code))
    except Exception as e:
        logs.append('방법6 웹스크래핑 오류: ' + str(e))

    if debug:
        return jsonify({'error': 'No captions', 'debug': logs}), 404
    return jsonify({'error': 'No captions available'}), 404


def extract_texts(content):
    import re
    texts = []

    # Format 3: <p> with <s> subtags
    p_matches = re.findall(r'<p [^>]*>[\s\S]*?</p>', content)
    if p_matches:
        for p in p_matches:
            s_matches = re.findall(r'<s[^>]*>([\s\S]*?)</s>', p)
            if s_matches:
                line = ''.join(s_matches)
            else:
                line = re.sub(r'<[^>]+>', '', p)
            line = (line.replace('&amp;', '&').replace('&lt;', '<')
                    .replace('&gt;', '>').replace('&#39;', "'")
                    .replace('&quot;', '"').strip())
            if line:
                texts.append(line)
        if texts:
            return texts

    # XML <text> format
    text_matches = re.findall(r'<text[^>]*>([\s\S]*?)</text>', content)
    if text_matches:
        for t in text_matches:
            clean = (t.replace('&amp;', '&').replace('&lt;', '<')
                     .replace('&gt;', '>').replace('&#39;', "'")
                     .replace('&quot;', '"').strip())
            if clean:
                texts.append(clean)
        if texts:
            return texts

    return texts


@app.route('/api/health', methods=['GET'])
def health_check():
    return jsonify({
        'status': 'healthy',
        'message': 'YouTube Summarizer API Server'
    })


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=True)
