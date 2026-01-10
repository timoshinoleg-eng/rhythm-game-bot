import os
import sqlite3
import jwt
import logging
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, Any, List

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv('DATABASE_URL', 'sqlite:///game.db')
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
JWT_SECRET = os.getenv('JWT_SECRET', 'your_super_secret_key_change_this_12345')
FRONTEND_URL = os.getenv('FRONTEND_URL', 'http://localhost:8000')

if not TELEGRAM_BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN не установлен в переменных окружения")

# Music configuration
MUSIC_FOLDER = "music"  # Папка с MP3 файлами
TRACKS_METADATA_FILE = "tracks.json"  # Метаданные треков

# Ensure music folder exists
os.makedirs(MUSIC_FOLDER, exist_ok=True)

# Global Application instance - will be initialized in lifespan
application: Optional[Application] = None

class GameResult(BaseModel):
    token: str
    score: int
    combo: int

class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path.replace('sqlite:///', '')
        self.init_db()
    
    def get_connection(self):
        return sqlite3.connect(self.db_path)
    
    def init_db(self):
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS players (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                best_score INTEGER DEFAULT 0,
                total_games INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                score INTEGER,
                combo INTEGER,
                song_id TEXT DEFAULT 'default',
                accuracy REAL DEFAULT 0,
                hit_count INTEGER DEFAULT 0,
                miss_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES players (user_id)
            )
        ''')
        
        # ЭТАП 7: Добавить новые колонки если их нет (для существующих БД)
        try:
            cursor.execute('ALTER TABLE results ADD COLUMN song_id TEXT DEFAULT "default"')
        except sqlite3.OperationalError:
            pass  # Колонка уже существует
        
        try:
            cursor.execute('ALTER TABLE results ADD COLUMN accuracy REAL DEFAULT 0')
        except sqlite3.OperationalError:
            pass
        
        try:
            cursor.execute('ALTER TABLE results ADD COLUMN hit_count INTEGER DEFAULT 0')
        except sqlite3.OperationalError:
            pass
        
        try:
            cursor.execute('ALTER TABLE results ADD COLUMN miss_count INTEGER DEFAULT 0')
        except sqlite3.OperationalError:
            pass
        
        conn.commit()
        conn.close()
        logger.info("База данных инициализирована")
    
    def get_or_create_player(self, user_id: int, username: Optional[str] = None) -> Dict[str, Any]:
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('SELECT * FROM players WHERE user_id = ?', (user_id,))
        player = cursor.fetchone()
        
        if not player:
            cursor.execute('''
                INSERT INTO players (user_id, username, best_score, total_games)
                VALUES (?, ?, 0, 0)
            ''', (user_id, username))
            conn.commit()
            
            cursor.execute('SELECT * FROM players WHERE user_id = ?', (user_id,))
            player = cursor.fetchone()
        
        conn.close()
        
        return {
            'user_id': player[0],
            'username': player[1],
            'best_score': player[2],
            'total_games': player[3],
            'created_at': player[4]
        }
    
    def save_game_result(self, user_id: int, score: int, combo: int) -> None:
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO results (user_id, score, combo)
            VALUES (?, ?, ?)
        ''', (user_id, score, combo))
        
        cursor.execute('SELECT best_score FROM players WHERE user_id = ?', (user_id,))
        current_best = cursor.fetchone()[0]
        
        if score > current_best:
            cursor.execute('''
                UPDATE players SET best_score = ?, total_games = total_games + 1
                WHERE user_id = ?
            ''', (score, user_id))
        else:
            cursor.execute('''
                UPDATE players SET total_games = total_games + 1
                WHERE user_id = ?
            ''', (user_id,))
        
        conn.commit()
        conn.close()
    
    def get_player_stats(self, user_id: int) -> Optional[Dict[str, Any]]:
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT best_score, total_games, 
                   (SELECT AVG(score) FROM results WHERE user_id = ?) as average_score
            FROM players WHERE user_id = ?
        ''', (user_id, user_id))
        
        result = cursor.fetchone()
        conn.close()
        
        if result:
            return {
                'best_score': result[0],
                'total_games': result[1],
                'average_score': round(result[2] or 0, 1)
            }
        return None
    
    def get_leaderboard(self, limit: int = 10) -> list:
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT p.user_id, p.username, p.best_score, p.total_games,
                   (SELECT COUNT(*) FROM players WHERE best_score > p.best_score) + 1 as rank
            FROM players p
            WHERE p.best_score > 0
            ORDER BY p.best_score DESC
            LIMIT ?
        ''', (limit,))
        
        results = cursor.fetchall()
        conn.close()
        
        leaderboard = []
        for i, row in enumerate(results):
            leaderboard.append({
                'rank': i + 1,
                'user_id': row[0],
                'username': row[1] or f'Player_{row[0]}',
                'best_score': row[2],
                'total_games': row[3]
            })
        
        return leaderboard
    
    def get_player_rank(self, user_id: int) -> int:
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT COUNT(*) + 1 FROM players 
            WHERE best_score > (SELECT best_score FROM players WHERE user_id = ?)
        ''', (user_id,))
        
        rank = cursor.fetchone()[0]
        conn.close()
        
        return rank

db = Database(DATABASE_URL)

class MusicManager:
    """Управление музыкальными треками"""
    
    def __init__(self):
        self.tracks_file = TRACKS_METADATA_FILE
        self.music_folder = MUSIC_FOLDER
        self.tracks = self.load_tracks()
    
    def load_tracks(self) -> List[Dict]:
        """Загрузить метаданные треков из JSON"""
        if not os.path.exists(self.tracks_file):
            # Создать дефолтный tracks.json
            default_tracks = [
                {
                    "id": 1,
                    "title": "Electronic Dreams",
                    "artist": "FMA Artist",
                    "bpm": 140,
                    "duration": 120,
                    "filename": "track1.mp3",
                    "genre": "Electronic",
                    "difficulty": "medium"
                },
                {
                    "id": 2,
                    "title": "Fast Beat",
                    "artist": "FMA Artist",
                    "bpm": 170,
                    "duration": 90,
                    "filename": "track2.mp3",
                    "genre": "Dance",
                    "difficulty": "hard"
                },
                {
                    "id": 3,
                    "title": "Chill Vibes",
                    "artist": "FMA Artist",
                    "bpm": 100,
                    "duration": 150,
                    "filename": "track3.mp3",
                    "genre": "Ambient",
                    "difficulty": "easy"
                }
            ]
            
            with open(self.tracks_file, 'w', encoding='utf-8') as f:
                json.dump(default_tracks, f, ensure_ascii=False, indent=2)
            
            logger.info(f"📁 Created default {self.tracks_file}")
            return default_tracks
        
        with open(self.tracks_file, 'r', encoding='utf-8') as f:
            return json.load(f)
    
    def get_all_tracks(self) -> List[Dict]:
        """Получить все доступные треки"""
        available_tracks = []
        
        for track in self.tracks:
            # Проверяем наличие файла
            file_path = os.path.join(self.music_folder, track['filename'])
            track_copy = track.copy()
            track_copy['available'] = os.path.exists(file_path)
            track_copy['url'] = f"/music/{track['filename']}" if track_copy['available'] else None
            available_tracks.append(track_copy)
        
        return available_tracks
    
    def get_track_by_id(self, track_id: int) -> Optional[Dict]:
        """Получить трек по ID"""
        for track in self.tracks:
            if track['id'] == track_id:
                file_path = os.path.join(self.music_folder, track['filename'])
                track_copy = track.copy()
                track_copy['available'] = os.path.exists(file_path)
                track_copy['url'] = f"/music/{track['filename']}" if track_copy['available'] else None
                return track_copy
        return None
    
    def add_track(self, title: str, artist: str, bpm: int, duration: int, 
                  filename: str, genre: str = "Unknown", difficulty: str = "medium") -> Dict:
        """Добавить новый трек"""
        new_id = max([t['id'] for t in self.tracks], default=0) + 1
        
        new_track = {
            "id": new_id,
            "title": title,
            "artist": artist,
            "bpm": bpm,
            "duration": duration,
            "filename": filename,
            "genre": genre,
            "difficulty": difficulty
        }
        
        self.tracks.append(new_track)
        self.save_tracks()
        
        return new_track
    
    def save_tracks(self):
        """Сохранить метаданные в JSON"""
        with open(self.tracks_file, 'w', encoding='utf-8') as f:
            json.dump(self.tracks, f, ensure_ascii=False, indent=2)

# Инициализация MusicManager
music_manager = MusicManager()

def generate_jwt_token(user_id: int) -> str:
    payload = {
        'user_id': user_id,
        'exp': datetime.utcnow() + timedelta(hours=24)
    }
    return jwt.encode(payload, JWT_SECRET, algorithm='HS256')

def verify_jwt_token(token: str) -> Optional[int]:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=['HS256'])
        return payload.get('user_id')
    except jwt.ExpiredSignatureError:
        logger.error("JWT token expired")
        return None
    except jwt.InvalidTokenError as e:
        logger.error(f"Invalid JWT token: {e}")
        return None

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    user_id = user.id
    username = user.username
    
    player = db.get_or_create_player(user_id, username)
    token = generate_jwt_token(user_id)
    
    # Game is served from FastAPI at /game (StaticFiles with index.html)
    # Token is passed as a query parameter and read by the frontend
    game_url = f"{FRONTEND_URL}/game?token={token}"
    
    keyboard = [
        [InlineKeyboardButton("🎵 Play Game", web_app={"url": game_url})]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    message_text = f"""
🎮 Добро пожаловать в Rhythm Game, {user.first_name}!

🎯 Правила игры:
• Падающие синие круги нужно тапать в зелёной зоне
• Perfect: +100 очков
• Good: +50 очков
• Miss: -10 HP

🏆 Ваш лучший счёт: {player['best_score']}
    """.strip()
    
    await update.message.reply_text(message_text, reply_markup=reply_markup)

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    stats = db.get_player_stats(user_id)
    
    if not stats:
        await update.message.reply_text("❌ Вы ещё не играли!")
        return
    
    message_text = f"""
📊 Ваша статистика:

🏆 Лучший счёт: {stats['best_score']}
🎮 Всего игр: {stats['total_games']}
📈 Средний счёт: {stats['average_score']}
    """.strip()
    
    await update.message.reply_text(message_text)

async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    leaderboard = db.get_leaderboard(10)
    
    if not leaderboard:
        await update.message.reply_text("🏆 Таблица лидеров пуста!")
        return
    
    medals = ['🥇', '🥈', '🥉', '4.', '5.', '6.', '7.', '8.', '9.', '10.']
    
    message_lines = ["🏆 Топ 10 игроков:\n"]
    
    for i, player in enumerate(leaderboard):
        medal = medals[i] if i < 3 else f"{i + 1}."
        username = player['username'][:15] + '...' if len(player['username']) > 15 else player['username']
        message_lines.append(f"{medal} {username} - {player['best_score']} очков")
    
    await update.message.reply_text('\n'.join(message_lines))

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message_text = """
🎮 Rhythm Game - Помощь

Команды:
/start - Начать игру
/stats - Показать статистику
/leaderboard - Таблица лидеров
/help - Эта помощь

Правила:
• Тапайте синие ноты в зелёной зоне
• Perfect: +100 очков
• Good: +50 очков
• Miss: -10 HP
• Игра заканчивается при HP ≤ 0

Удачи! 🎵
    """.strip()
    
    await update.message.reply_text(message_text)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Lifespan context manager для управления жизненным циклом Application.
    Инициализирует и запускает Application при старте FastAPI,
    корректно останавливает при завершении.
    """
    global application
    
    # Initialize Application
    logger.info("Инициализация Telegram Bot Application...")
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    # Add command handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("leaderboard", leaderboard_command))
    application.add_handler(CommandHandler("help", help_command))
    
    # Initialize and start Application
    await application.initialize()
    await application.start()
    logger.info("Telegram Bot Application запущен и готов к работе")
    
    yield
    
    # Cleanup: stop and shutdown Application
    logger.info("Остановка Telegram Bot Application...")
    await application.stop()
    await application.shutdown()
    logger.info("Telegram Bot Application остановлен")

# Create FastAPI app with lifespan
app = FastAPI(title="Rhythm Game Bot", lifespan=lifespan)

# Add middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve static frontend for the rhythm game
# /game -> serves game/index.html
# /game/* -> serves other files from game/ directory
app.mount("/game", StaticFiles(directory="game", html=True), name="game")

@app.post("/webhook")
async def webhook(request: Request) -> JSONResponse:
    """
    Webhook endpoint для получения обновлений от Telegram.
    Application должен быть инициализирован через lifespan context manager.
    """
    global application
    
    if application is None:
        logger.error("Application не инициализирован!")
        return JSONResponse(
            content={"status": "error", "message": "Application not initialized"},
            status_code=503
        )
    
    try:
        data = await request.json()
        update = Update.de_json(data, application.bot)
        await application.process_update(update)
        return JSONResponse(content={"status": "ok"})
    except Exception as e:
        logger.error(f"Webhook error: {e}", exc_info=True)
        return JSONResponse(
            content={"status": "error", "message": str(e)},
            status_code=500
        )

@app.post("/game-result")
@app.post("/api/game-result")
async def game_result(result: GameResult) -> JSONResponse:
    try:
        user_id = verify_jwt_token(result.token)
        
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid or expired token")
        
        db.save_game_result(user_id, result.score, result.combo)
        rank = db.get_player_rank(user_id)
        
        messages = [
            "Great job!",
            "Awesome!",
            "Fantastic!",
            "Excellent!",
            "Amazing!"
        ]
        
        message = messages[min(result.score // 500, len(messages) - 1)]
        
        return JSONResponse(content={
            "status": "ok",
            "rank": rank,
            "message": f"{message} Rank: #{rank}"
        })
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error saving result: {e}")
        return JSONResponse(content={
            "status": "error",
            "message": "Internal server error"
        }, status_code=500)

@app.post("/api/analytics")
async def analytics(request: Request) -> JSONResponse:
    """
    Lightweight analytics endpoint.

    Frontend sends JSON via navigator.sendBeacon or fetch to /api/analytics.
    Optionally includes JWT token as query param (?token=...) so we can
    associate events with a Telegram user.
    """
    try:
        raw_body = await request.body()
        data: Optional[Dict[str, Any]] = None

        if raw_body:
            try:
                data = json.loads(raw_body.decode("utf-8"))
            except json.JSONDecodeError:
                # If payload is not valid JSON, log raw body and continue
                logger.warning("Received non-JSON analytics payload")

        token = request.query_params.get("token")
        user_id = verify_jwt_token(token) if token else None

        logger.info(
            "Analytics event",
            extra={
                "user_id": user_id,
                "remote_addr": request.client.host if request.client else None,
                "payload": data,
            },
        )

        return JSONResponse(content={"status": "ok"})
    except Exception as e:
        logger.error(f"Analytics error: {e}")
        return JSONResponse(
            content={"status": "error", "message": "Internal server error"},
            status_code=500,
        )

# ============================================================================
# MUSIC API ENDPOINTS
# ============================================================================

@app.get("/api/tracks")
async def get_tracks():
    """Получить список всех доступных треков"""
    tracks = music_manager.get_all_tracks()
    return {
        "success": True,
        "tracks": tracks,
        "total": len(tracks)
    }

@app.get("/api/tracks/{track_id}")
async def get_track(track_id: int):
    """Получить информацию о треке по ID"""
    track = music_manager.get_track_by_id(track_id)
    
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    
    return {
        "success": True,
        "track": track
    }

@app.get("/music/{filename}")
async def serve_music(filename: str):
    """Раздача MP3 файлов"""
    file_path = os.path.join(MUSIC_FOLDER, filename)
    
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Music file not found")
    
    return FileResponse(
        file_path,
        media_type="audio/mpeg",
        headers={
            "Accept-Ranges": "bytes",
            "Cache-Control": "public, max-age=3600"
        }
    )

@app.post("/api/admin/tracks")
async def add_new_track(
    title: str,
    artist: str,
    bpm: int,
    duration: int,
    filename: str,
    genre: str = "Unknown",
    difficulty: str = "medium"
):
    """Добавить новый трек (для админов)"""
    track = music_manager.add_track(
        title=title,
        artist=artist,
        bpm=bpm,
        duration=duration,
        filename=filename,
        genre=genre,
        difficulty=difficulty
    )
    
    return {
        "success": True,
        "track": track,
        "message": f"Track '{title}' added successfully"
    }

# ============================================================================
# ЭТАП 6: USER TRACK UPLOAD ENDPOINTS
# ============================================================================

from fastapi import UploadFile, File, Form
from pathlib import Path
import datetime

# Создать папку для user tracks
USER_TRACKS_DIR = Path("user_tracks")
USER_TRACKS_DIR.mkdir(exist_ok=True)

@app.post("/api/upload-track")
async def upload_track(
    token: str = Form(...),
    file: UploadFile = File(...),
    title: str = Form(""),
    artist: str = Form("")
):
    """
    Загрузить MP3 файл пользователя
    """
    try:
        # Валидация токена
        user_id = verify_jwt_token(token)
        if not user_id:
            return JSONResponse(content={"error": "Invalid token"}, status_code=401)
        
        # Валидация файла
        if file.content_type not in ["audio/mpeg", "audio/mp3"]:
            return JSONResponse(content={"error": "Only MP3 files allowed"}, status_code=400)
        
        # Проверка размера файла (50MB max)
        file_content = await file.read()
        if len(file_content) > 50 * 1024 * 1024:
            return JSONResponse(content={"error": "File too large (max 50MB)"}, status_code=400)
        
        # Сохранить файл
        filename = f"{user_id}_{file.filename}"
        filepath = USER_TRACKS_DIR / filename
        
        with open(filepath, "wb") as f:
            f.write(file_content)
        
        # Сохранить метаданные в JSON
        track_data = {
            "id": f"user_{user_id}_{filename}",
            "title": title or file.filename.replace('.mp3', ''),
            "artist": artist or "Unknown Artist",
            "filename": filename,
            "filepath": str(filepath),
            "bpm": 140,  # Default BPM, можно позволить пользователю задать
            "duration": 180,  # Получить из файла позже
            "isUserTrack": True,
            "uploadedBy": user_id,
            "uploadedAt": datetime.datetime.now().isoformat()
        }
        
        # Сохранить в database или файл
        user_tracks_file = USER_TRACKS_DIR / f"{user_id}_tracks.json"
        tracks = []
        if user_tracks_file.exists():
            with open(user_tracks_file, 'r', encoding='utf-8') as f:
                tracks = json.load(f)
        
        tracks.append(track_data)
        
        with open(user_tracks_file, 'w', encoding='utf-8') as f:
            json.dump(tracks, f, ensure_ascii=False, indent=2)
        
        return JSONResponse(content={
            "success": True,
            "track": track_data
        })
    
    except Exception as e:
        logger.error(f"Error uploading track: {e}")
        return JSONResponse(content={"error": str(e)}, status_code=500)


@app.get("/api/user-tracks")
async def get_user_tracks(token: str):
    """
    Получить список всех песен пользователя
    """
    try:
        user_id = verify_jwt_token(token)
        if not user_id:
            return JSONResponse(content={"error": "Invalid token"}, status_code=401)
        
        user_tracks_file = USER_TRACKS_DIR / f"{user_id}_tracks.json"
        
        if not user_tracks_file.exists():
            return JSONResponse(content={"tracks": []})
        
        with open(user_tracks_file, 'r', encoding='utf-8') as f:
            tracks = json.load(f)
        
        return JSONResponse(content={"tracks": tracks})
    
    except Exception as e:
        logger.error(f"Error getting user tracks: {e}")
        return JSONResponse(content={"error": str(e)}, status_code=500)


@app.get("/api/track-file/{filename}")
async def get_track_file(filename: str):
    """
    Получить файл песни для проигрывания
    """
    try:
        filepath = USER_TRACKS_DIR / filename
        
        if not filepath.exists():
            raise HTTPException(status_code=404, detail="File not found")
        
        return FileResponse(
            filepath,
            media_type="audio/mpeg",
            headers={
                "Accept-Ranges": "bytes",
                "Cache-Control": "public, max-age=3600"
            }
        )
    
    except Exception as e:
        logger.error(f"Error serving track file: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ============================================================================
# ЭТАП 7: PERSONAL BEST ENDPOINTS
# ============================================================================

@app.get("/api/personal-best")
async def get_personal_best(token: str, songId: str):
    """
    Получить личный рекорд для песни
    """
    try:
        user_id = verify_jwt_token(token)
        if not user_id:
            return JSONResponse(content={"error": "Invalid token"}, status_code=401)
        
        # Получить из database
        conn = db.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT score, accuracy, combo, hit_count, miss_count
            FROM results
            WHERE user_id = ? AND song_id = ?
            ORDER BY score DESC
            LIMIT 1
        ''', (user_id, songId))
        
        result = cursor.fetchone()
        conn.close()
        
        if result:
            return JSONResponse(content={
                "personalBest": {
                    "score": result[0],
                    "accuracy": result[1],
                    "combo": result[2],
                    "hitCount": result[3],
                    "missCount": result[4]
                }
            })
        else:
            return JSONResponse(content={"personalBest": None})
    
    except Exception as e:
        logger.error(f"Error getting personal best: {e}")
        return JSONResponse(content={"error": str(e)}, status_code=500)


@app.post("/api/save-score")
async def save_score(request: Request):
    """
    Сохранить результат игры
    """
    try:
        data = await request.json()
        token = data.get('token')
        user_id = verify_jwt_token(token)
        
        if not user_id:
            return JSONResponse(content={"error": "Invalid token"}, status_code=401)
        
        song_id = data.get('songId', 'default')
        score = data.get('score', 0)
        accuracy = data.get('accuracy', 0)
        combo = data.get('combo', 0)
        hit_count = data.get('hitCount', 0)
        miss_count = data.get('missCount', 0)
        
        # Получить текущий рекорд
        conn = db.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT score FROM results
            WHERE user_id = ? AND song_id = ?
            ORDER BY score DESC
            LIMIT 1
        ''', (user_id, song_id))
        
        current_best = cursor.fetchone()
        is_new_pb = not current_best or score > current_best[0]
        
        # Сохранить результат
        cursor.execute('''
            INSERT INTO results (user_id, score, combo, song_id, accuracy, hit_count, miss_count)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (user_id, score, combo, song_id, accuracy, hit_count, miss_count))
        
        # Обновить best_score если это новый рекорд
        if is_new_pb:
            cursor.execute('''
                UPDATE players SET best_score = ?
                WHERE user_id = ? AND (best_score < ? OR best_score IS NULL)
            ''', (score, user_id, score))
        
        conn.commit()
        conn.close()
        
        return JSONResponse(content={
            "success": True,
            "isNewPersonalBest": is_new_pb
        })
    
    except Exception as e:
        logger.error(f"Error saving score: {e}")
        return JSONResponse(content={"error": str(e)}, status_code=500)

@app.get("/health")
async def health_check() -> JSONResponse:
    return JSONResponse(content={
        "status": "ok",
        "message": "Bot is running"
    })

@app.get("/")
async def root() -> JSONResponse:
    return JSONResponse(content={
        "message": "Rhythm Game Bot API",
        "endpoints": {
            "webhook": "/webhook",
            "game_result": "/game-result",
            "health": "/health",
            "game": "/game.html"
        }
    })

if __name__ == "__main__":
    import uvicorn
    
    logger.info("Запуск Rhythm Game Bot...")
    logger.info(f"Database: {DATABASE_URL}")
    logger.info(f"Frontend URL: {FRONTEND_URL}")
    
    uvicorn.run(app, host="0.0.0.0", port=8000)