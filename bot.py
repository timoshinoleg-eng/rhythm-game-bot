import os
import sqlite3
import jwt
import logging
import json
from datetime import datetime, timedelta
from typing import Optional, Dict, Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
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

app = FastAPI(title="Rhythm Game Bot")

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
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES players (user_id)
            )
        ''')
        
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

@app.post("/webhook")
async def webhook(request: Request) -> JSONResponse:
    try:
        update = Update.de_json(await request.json(), application.bot)
        await application.process_update(update)
        return JSONResponse(content={"status": "ok"})
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return JSONResponse(content={"status": "error", "message": str(e)}, status_code=500)

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

application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

application.add_handler(CommandHandler("start", start_command))
application.add_handler(CommandHandler("stats", stats_command))
application.add_handler(CommandHandler("leaderboard", leaderboard_command))
application.add_handler(CommandHandler("help", help_command))

if __name__ == "__main__":
    import uvicorn
    
    logger.info("Запуск Rhythm Game Bot...")
    logger.info(f"Database: {DATABASE_URL}")
    logger.info(f"Frontend URL: {FRONTEND_URL}")
    
    uvicorn.run(app, host="0.0.0.0", port=8000)