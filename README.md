## Rhythm Game Telegram Bot

This project is a Telegram bot with a built-in rhythm game frontend served by FastAPI.

### Requirements

- Python 3.10+
- `pip` for installing dependencies

### Installation

1. **Clone or unpack the project** into a directory, e.g.:

   ```bash
   cd project-bolt-sb1-vqcqsu6m
   ```

2. **Create and activate a virtual environment** (optional but recommended):

   ```bash
   python -m venv .venv
   .venv\Scripts\activate  # on Windows
   # source .venv/bin/activate  # on macOS / Linux
   ```

3. **Install dependencies**:

   ```bash
   pip install -r requirements.txt
   ```

4. **Create a `.env` file** in the project root with at least:

   ```env
   TELEGRAM_BOT_TOKEN=YOUR_TELEGRAM_BOT_TOKEN_HERE
   JWT_SECRET=some_long_random_secret_string
   FRONTEND_URL=http://localhost:8000
   DATABASE_URL=sqlite:///game.db
   ```

### Running the backend locally

Start the FastAPI app with Uvicorn:

```bash
uvicorn bot:app --reload --port 8000
```

FastAPI will expose:

- **Game frontend**: `http://localhost:8000/game?token=test`  
  (the bot normally passes a real JWT token in the `token` query parameter)
- **Webhook endpoint for Telegram**: `POST /webhook`
- **Game result endpoint**: `POST /api/game-result`
- **Analytics endpoint**: `POST /api/analytics`
- **Health check**: `GET /health`

### Game integration details

- The game frontend is a self-contained HTML file at `game/index.html`.
- It is served under the `/game` path by FastAPI using `StaticFiles`.
- The Telegram bot generates a JWT for each user and opens:

  ```text
  {FRONTEND_URL}/game?token={JWT}
  ```

- The game:
  - Reads the `token` from the URL query string.
  - Sends gameplay analytics events to `POST /api/analytics` (optionally including the JWT as a query parameter).
  - Posts final game results (score and combo) to `POST /api/game-result` with JSON:

    ```json
    {
      "token": "<JWT from URL>",
      "score": 12345,
      "combo": 42
    }
    ```

### Telegram bot commands

The bot currently supports:

- `/start` – starts the game and sends the WebApp button.
- `/stats` – shows your personal statistics.
- `/leaderboard` – shows the top players.
- `/help` – shows basic help and rules.


