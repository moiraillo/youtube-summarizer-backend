"""
YouTube 투자 요약기 - 백엔드 서버
- 자막 추출
- Google OAuth 로그인
- MongoDB 사용자 데이터 저장
- API 키 서버 관리
"""

import os
import certifi
import asyncio
import edge_tts
import io
from flask import Flask, request, jsonify, session, send_file
from flask_cors import CORS
from youtube_transcript_api import YouTubeTranscriptApi
from dotenv import load_dotenv
from pymongo import MongoClient
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
import requests

# 환경변수 로드
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'dev-secret-key-change-in-production')

# CORS 설정 (프론트엔드 URL 허용)
FRONTEND_URL = os.getenv('FRONTEND_URL', 'http://localhost:8001')
CORS(app, supports_credentials=True, origins=[
    FRONTEND_URL,
    'http://localhost:8001',
    'http://localhost:3000'
])

# MongoDB 연결 (certifi SSL 인증서 사용)
MONGO_URI = os.getenv('MONGO_URI', 'mongodb://localhost:27017/')
client = MongoClient(MONGO_URI, tlsCAFile=certifi.where())
db = client['youtube_summarizer']
users_collection = db['users']

# API 키 (서버 환경변수에서 관리)
YOUTUBE_API_KEY = os.getenv('YOUTUBE_API_KEY')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
GOOGLE_CLIENT_ID = os.getenv('GOOGLE_CLIENT_ID')


# ============================================
# 자막 추출 API
# ============================================

@app.route('/api/transcript/<video_id>', methods=['GET'])
def get_transcript(video_id):
    """YouTube 영상의 자막을 가져옵니다."""
    try:
        api = YouTubeTranscriptApi()
        try:
            transcript_obj = api.fetch(video_id, languages=['ko'])
        except:
            try:
                transcript_obj = api.fetch(video_id, languages=['en'])
            except:
                transcript_obj = api.fetch(video_id)
        
        full_text = ' '.join([snippet.text for snippet in transcript_obj.snippets])
        
        return jsonify({
            'success': True,
            'transcript': full_text,
            'video_id': video_id
        })
        
    except Exception as e:
        print(f"자막 추출 실패: {str(e)}")
        return jsonify({
            'success': False,
            'error': '자막을 찾을 수 없습니다',
            'video_id': video_id
        }), 404


# ============================================
# Google OAuth 로그인
# ============================================

@app.route('/api/auth/google', methods=['POST'])
def google_login():
    """Google ID 토큰을 검증하고 사용자 정보 저장"""
    try:
        token = request.json.get('token')
        
        if not token:
            return jsonify({'error': 'Token required'}), 400
        
        idinfo = id_token.verify_oauth2_token(
            token, 
            google_requests.Request(), 
            GOOGLE_CLIENT_ID
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
                'channels': [],
                'created_at': None
            }
            users_collection.insert_one(user)
        
        session['user_id'] = user_id
        
        return jsonify({
            'success': True,
            'user': {
                'user_id': user_id,
                'email': email,
                'name': name,
                'picture': picture,
                'channels': user.get('channels', [])
            }
        })
        
    except ValueError as e:
        return jsonify({'error': 'Invalid token'}), 401
    except Exception as e:
        print(f"로그인 오류: {str(e)}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/auth/logout', methods=['POST'])
def logout():
    """로그아웃"""
    session.pop('user_id', None)
    return jsonify({'success': True})


@app.route('/api/auth/check', methods=['GET'])
def check_auth():
    """로그인 상태 확인"""
    user_id = session.get('user_id')
    
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


# ============================================
# 채널 관리 API
# ============================================

@app.route('/api/channels', methods=['GET'])
def get_channels():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'error': 'Unauthorized'}), 401
    user = users_collection.find_one({'user_id': user_id})
    return jsonify({
        'channels': user.get('channels', []) if user else []
    })


@app.route('/api/channels', methods=['POST'])
def add_channel():
    user_id = session.get('user_id')
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
    user_id = session.get('user_id')
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


# ============================================
# API 키 제공
# ============================================

@app.route('/api/keys', methods=['GET'])
def get_api_keys():
    return jsonify({
        'youtube': YOUTUBE_API_KEY,
        'gemini': GEMINI_API_KEY
    })


# ============================================
# Gemini 요약 프록시
# ============================================

@app.route('/api/summarize', methods=['POST'])
def summarize_video():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        data = request.json
        video_title = data.get('title')
        channel = data.get('channel')
        transcript = data.get('transcript')
        
        gemini_url = f'https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}'
        
        prompt = f"""다음은 투자 관련 YouTube 영상의 자막입니다:

제목: {video_title}
채널: {channel}
자막 내용:
{transcript}

# 목적
이 영상에서 투자자가 반드시 알아야 할 핵심 정보만 요약해주세요.
농담, 광고, 인사말, 투자와 무관한 내용은 모두 제외하세요.
**특정 기업이나 특정 섹터에 대한 의견은 절대 생략하지 말고 반드시 포함하세요.**

# 요약 규칙
1. **구어체** 사용 (입니다, 합니다 등)
2. **괄호 () 절대 금지** - TTS로 읽을 때 어색함
3. 각 카테고리당 **2-3문장 이내**로 간결하게
4. **구체적인 수치, 날짜, 종목명, 섹터명** 포함
5. **특정 기업명이나 섹터명이 언급되면 반드시 요약에 포함**

# 요약 카테고리

## 1. 시장 분석
- 주식 시장 전반적인 흐름, 추세, 전망
- 특정 섹터의 시장 전망

## 2. 종목 추천
- 특정 기업/종목 및 섹터 투자 의견
- 매수/매도/보유 의견 포함

## 3. 리스크/주의사항
- 투자 시 주의해야 할 위험 요소
- 특정 기업이나 섹터의 리스크

## 4. 기타 인사이트
- 투자 전략, 포트폴리오 구성 팁
- 특정 기업이나 섹터 관련 인사이트

위 형식으로 투자 핵심 정보만 간결하게 요약해주세요."""
        
        response = requests.post(
            gemini_url,
            json={
                'contents': [{
                    'parts': [{'text': prompt}]
                }]
            }
        )
        
        result = response.json()
        
        if 'error' in result:
            return jsonify({'error': result['error']['message']}), 400
        
        summary_text = result['candidates'][0]['content']['parts'][0]['text']
        
        return jsonify({
            'success': True,
            'summary': summary_text
        })
        
    except Exception as e:
        print(f"요약 오류: {str(e)}")
        return jsonify({'error': str(e)}), 500


# ============================================
# Edge TTS (AI 음성 생성)
# ============================================

@app.route('/api/tts', methods=['POST'])
def text_to_speech():
    """Edge TTS로 텍스트를 자연스러운 AI 음성으로 변환"""
    try:
        data = request.json
        text = data.get('text', '')
        voice = data.get('voice', 'ko-KR-SunHiNeural')  # 기본: 여성 음성
        
        if not text:
            return jsonify({'error': 'Text required'}), 400
        
        # edge-tts로 음성 생성
        async def generate():
            communicate = edge_tts.Communicate(text, voice, rate='+10%')
            audio_data = b''
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    audio_data += chunk["data"]
            return audio_data
        
        audio_data = asyncio.run(generate())
        
        # MP3 파일로 반환
        return send_file(
            io.BytesIO(audio_data),
            mimetype='audio/mpeg',
            as_attachment=False,
            download_name='tts.mp3'
        )
        
    except Exception as e:
        print(f"TTS 오류: {str(e)}")
        return jsonify({'error': str(e)}), 500


# ============================================
# 헬스 체크
# ============================================

@app.route('/api/health', methods=['GET'])
def health_check():
    return jsonify({
        'status': 'healthy',
        'message': 'YouTube Summarizer API Server'
    })


# ============================================
# 서버 실행
# ============================================

if __name__ == '__main__':
    print("\n" + "="*60)
    print("🚀 YouTube 투자 요약기 백엔드 서버 시작")
    print("="*60)
    print(f"📡 주소: http://localhost:5001")
    print(f"✅ 자막 API: http://localhost:5001/api/transcript/<video_id>")
    print(f"🔐 로그인 API: http://localhost:5001/api/auth/google")
    print(f"📺 채널 API: http://localhost:5001/api/channels")
    print(f"🔑 API 키: http://localhost:5001/api/keys")
    print("\n서버를 종료하려면 Ctrl + C 를 누르세요.\n")
    print("="*60 + "\n")
    
    app.run(
        host='0.0.0.0',
        port=5001,
        debug=True
    )
