import os
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import psycopg

app = Flask(__name__, static_folder="static", static_url_path="")
CORS(app)

DB_DSN = os.environ.get("DATABASE_URL", "dbname=liam user=liam")


def get_db():
    return psycopg.connect(DB_DSN, row_factory=psycopg.rows.dict_row)


def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS basketball_records (
                id SERIAL PRIMARY KEY,
                category TEXT NOT NULL CHECK (category IN ('1_hand_accuracy','1_hand_high_score','2_hand_accuracy','2_hand_high_score')),
                player_name TEXT NOT NULL,
                score INTEGER NOT NULL,
                verified_by TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS foosball_players (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL UNIQUE
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS foosball_matches (
                id SERIAL PRIMARY KEY,
                player_id INTEGER NOT NULL REFERENCES foosball_players(id) ON DELETE CASCADE,
                opponent TEXT NOT NULL,
                player_score INTEGER NOT NULL,
                opponent_score INTEGER NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_basketball_category_score
            ON basketball_records(category, score DESC)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_foosball_matches_player
            ON foosball_matches(player_id)
        """)


# ── Basketball ────────────────────────────────────────────────────────────────

CATEGORIES = [
    {
        "slug": "1_hand_accuracy",
        "name": "1 Hand Accuracy",
        "rule": "Most consecutive shots made without missing, using one hand.",
    },
    {
        "slug": "1_hand_high_score",
        "name": "1 Hand High Score",
        "rule": "Highest score in a 30-second match, using one hand.",
    },
    {
        "slug": "2_hand_accuracy",
        "name": "2 Hand Accuracy",
        "rule": "Most consecutive shots made without missing, using two hands.",
    },
    {
        "slug": "2_hand_high_score",
        "name": "2 Hand High Score",
        "rule": "Highest score in a 30-second match, using two hands.",
    },
]


@app.get("/api/basketball/categories")
def basketball_categories():
    return jsonify(CATEGORIES)


@app.route("/api/basketball/records", methods=["GET", "POST"])
def basketball_records():
    if request.method == "GET":
        category = request.args.get("category", "")
        if not category:
            return jsonify({"error": "category is required"}), 400

        with get_db() as conn:
            rows = conn.execute(
                """SELECT id, category, player_name, score, verified_by, created_at
                   FROM basketball_records
                   WHERE category = %s
                   ORDER BY score DESC""",
                [category],
            ).fetchall()

        return jsonify(rows)

    if request.method == "POST":
        data = request.get_json(silent=True)
        if not data:
            return jsonify({"error": "request body is required"}), 400

        required = ["category", "player_name", "score", "verified_by"]
        missing = [f for f in required if not data.get(f)]
        if missing:
            return jsonify({"error": f"missing fields: {', '.join(missing)}"}), 400

        try:
            score = int(data["score"])
        except (TypeError, ValueError):
            return jsonify({"error": "score must be an integer"}), 400

        valid_categories = [c["slug"] for c in CATEGORIES]
        if data["category"] not in valid_categories:
            return jsonify({"error": f"invalid category, must be one of: {', '.join(valid_categories)}"}), 400

        with get_db() as conn:
            row = conn.execute(
                """INSERT INTO basketball_records (category, player_name, score, verified_by)
                   VALUES (%s, %s, %s, %s)
                   RETURNING id, category, player_name, score, verified_by, created_at""",
                [data["category"], data["player_name"], score, data["verified_by"]],
            ).fetchone()

        return jsonify(row), 201


# ── Foosball ──────────────────────────────────────────────────────────────────


@app.route("/api/foosball/players", methods=["GET", "POST"])
def foosball_players():
    if request.method == "GET":
        with get_db() as conn:
            rows = conn.execute(
                """SELECT p.id, p.name, COUNT(m.id) AS match_count
                   FROM foosball_players p
                   LEFT JOIN foosball_matches m ON m.player_id = p.id
                   GROUP BY p.id, p.name
                   ORDER BY p.name"""
            ).fetchall()
        return jsonify(rows)

    if request.method == "POST":
        data = request.get_json(silent=True)
        if not data or not data.get("name"):
            return jsonify({"error": "name is required"}), 400

        name = data["name"].strip()
        if not name:
            return jsonify({"error": "name is required"}), 400

        with get_db() as conn:
            try:
                row = conn.execute(
                    "INSERT INTO foosball_players (name) VALUES (%s) RETURNING id, name",
                    [name],
                ).fetchone()
            except psycopg.errors.UniqueViolation:
                return jsonify({"error": "player already exists"}), 409

        return jsonify(row), 201


@app.get("/api/foosball/players/<int:player_id>/matches")
def player_matches(player_id):
    with get_db() as conn:
        player = conn.execute(
            "SELECT id, name FROM foosball_players WHERE id = %s", [player_id]
        ).fetchone()

        if not player:
            return jsonify({"error": "player not found"}), 404

        matches = conn.execute(
            """SELECT id, opponent, player_score, opponent_score, created_at
               FROM foosball_matches
               WHERE player_id = %s
               ORDER BY created_at DESC""",
            [player_id],
        ).fetchall()

        stats = conn.execute(
            """SELECT
                 COUNT(*) AS total_matches,
                 COUNT(*) FILTER (WHERE player_score > opponent_score) AS wins,
                 COUNT(*) FILTER (WHERE player_score < opponent_score) AS losses,
                 COUNT(*) FILTER (WHERE player_score = opponent_score) AS draws
               FROM foosball_matches
               WHERE player_id = %s""",
            [player_id],
        ).fetchone()

    return jsonify({"player": player, "matches": matches, "stats": stats})


@app.post("/api/foosball/matches")
def create_match():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "request body is required"}), 400

    required = ["player_id", "opponent", "player_score", "opponent_score"]
    missing = [f for f in required if data.get(f) is None]
    if missing:
        return jsonify({"error": f"missing fields: {', '.join(missing)}"}), 400

    try:
        player_id = int(data["player_id"])
        player_score = int(data["player_score"])
        opponent_score = int(data["opponent_score"])
    except (TypeError, ValueError):
        return jsonify({"error": "player_id and scores must be integers"}), 400

    with get_db() as conn:
        submitting_player = conn.execute(
            "SELECT id, name FROM foosball_players WHERE id = %s", [player_id]
        ).fetchone()
        if not submitting_player:
            return jsonify({"error": "player not found"}), 404

        opponent_player = conn.execute(
            "SELECT id FROM foosball_players WHERE name = %s", [data["opponent"]]
        ).fetchone()

        row = conn.execute(
            """INSERT INTO foosball_matches (player_id, opponent, player_score, opponent_score)
               VALUES (%s, %s, %s, %s)
               RETURNING id, player_id, opponent, player_score, opponent_score, created_at""",
            [player_id, data["opponent"], player_score, opponent_score],
        ).fetchone()

        if opponent_player and opponent_player["id"] != player_id:
            conn.execute(
                """INSERT INTO foosball_matches (player_id, opponent, player_score, opponent_score)
                   VALUES (%s, %s, %s, %s)""",
                [opponent_player["id"], submitting_player["name"], opponent_score, player_score],
            )

    return jsonify(row), 201


# ── Static files ──────────────────────────────────────────────────────────────


@app.route("/")
def serve_index():
    return send_from_directory("static", "index.html")


if __name__ == "__main__":
    init_db()
    app.run(host="127.0.0.1", port=5000, debug=True)
