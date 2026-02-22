사용자 데이터 저장
- API 키 서버 관리
"""

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
        print(f"로그인 오류: {str(e)}")
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
    """Edge TTS를 사용한 음성 생성"""
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
        print(f"TTS 오류: {str(e)}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/health', methods=['GET'])
def health_check():
    return jsonify({
        'status': 'healthy',
        'message': 'YouTube Summarizer API Server'
    })


if __name__ == '__main__':
    print("🚀 YouTube 투자 요약기 백엔드 서버 시작")
    print(f"📡 주소: http://localhost:5001")
    app.run(host='0.0.0.0', port=5001, debug=True)
