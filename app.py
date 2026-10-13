import os
import re
import sqlite3
import unicodedata
import uuid
from datetime import datetime
import random

import requests
from flask import Flask, g, redirect, render_template, request, url_for

app = Flask(__name__)

# Com DATABASE_URL definida (Neon/PostgreSQL), o app usa Postgres.
# Sem ela, usa o arquivo SQLite local (útil para testar no computador).
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "bolao.db"))
FOOTBALL_DATA_TOKEN = os.environ.get("FOOTBALL_DATA_TOKEN", "")

SEED_PLAYERS = [
    ("Da Cruz", 0), ("Marlan", 0), ("Dudu", 0), ("Spinkfloyd", 0), ("Gokulandia", 0),
    ("Will", 0), ("Miguel", 0), ("Tairony", 0), ("Douglas", 0), ("Erikson", 0),
    ("Gusta", 0), ("Andrey", 0), ("André", 0), ("Fabrício", 0), ("Janderson", 0),
    ("Pedro", 0), ("Andersu", 0), ("Adanilson", 0), ("Erick", 0), ("Shiva", 0),
    ("Júlio", 0), ("João Felipe", 0), ("Emanoel", 0),
]

COMPETITION_CODE = "CL"
COMPETITION_NAME = "Champions League"


class Row(dict):
    """Linha de resultado que aceita row["coluna"] e row[0], como o sqlite3.Row."""

    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


def _pg_row_factory(cursor):
    names = [c.name for c in (cursor.description or [])]

    def make_row(values):
        return Row(zip(names, values))

    return make_row


class DB:
    """Pequeno adaptador para o código funcionar igual em SQLite e PostgreSQL.

    O código escreve as consultas com "?"; no Postgres elas viram "%s".
    """

    def __init__(self, conn, is_pg):
        self.conn = conn
        self.is_pg = is_pg

    def execute(self, sql, args=()):
        if self.is_pg:
            sql = sql.replace("%", "%%").replace("?", "%s")
        return self.conn.execute(sql, tuple(args))

    def commit(self):
        self.conn.commit()

    def close(self):
        self.conn.close()


def connect_db():
    if DATABASE_URL:
        import psycopg

        conn = psycopg.connect(DATABASE_URL, row_factory=_pg_row_factory, connect_timeout=15)
        return DB(conn, True)
    folder = os.path.dirname(DB_PATH)
    if folder:
        os.makedirs(folder, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return DB(conn, False)


def get_db():
    if "db" not in g:
        g.db = connect_db()
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


SCHEMA = [
    """CREATE TABLE IF NOT EXISTS players (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        starting_points INTEGER NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS rounds (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS matches (
        id TEXT PRIMARY KEY,
        round_id TEXT NOT NULL REFERENCES rounds(id) ON DELETE CASCADE,
        home TEXT NOT NULL,
        away TEXT NOT NULL,
        home_goals INTEGER,
        away_goals INTEGER,
        position INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS picks (
        round_id TEXT NOT NULL,
        player_id TEXT NOT NULL,
        match_id TEXT NOT NULL,
        home_guess INTEGER NOT NULL,
        away_guess INTEGER NOT NULL,
        PRIMARY KEY (round_id, player_id, match_id)
    )""",
    """CREATE TABLE IF NOT EXISTS tournament_rounds (
        id TEXT PRIMARY KEY,
        number INTEGER NOT NULL,
        phase TEXT NOT NULL,
        football_round_id TEXT REFERENCES rounds(id) ON DELETE CASCADE,
        created_at TEXT NOT NULL,
        skipped INTEGER NOT NULL DEFAULT 0,
        UNIQUE(phase, number)
    )""",
    """CREATE TABLE IF NOT EXISTS tournament_matchups (
        id TEXT PRIMARY KEY,
        tournament_round_id TEXT NOT NULL REFERENCES tournament_rounds(id) ON DELETE CASCADE,
        player1_id TEXT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
        player2_id TEXT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
        result TEXT,
        player1_score INTEGER,
        player2_score INTEGER
    )""",
]


def init_db():
    db = connect_db()
    try:
        if db.is_pg:
            # Evita que dois processos criem as tabelas ao mesmo tempo.
            db.execute("SELECT pg_advisory_lock(727274)")
            exists = db.execute(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_name = 'players'"
            ).fetchone()
        else:
            exists = db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='players'"
            ).fetchone()
        # Os jogadores iniciais só são cadastrados quando o banco é criado pela
        # primeira vez. Assim, "remover todos os jogadores" não os recria ao
        # reiniciar o servidor.
        is_new_db = exists is None

        for stmt in SCHEMA:
            db.execute(stmt)

        # Migração: bancos criados antes da função "Rodada sem jogos".
        if db.is_pg:
            db.execute("ALTER TABLE tournament_rounds ADD COLUMN IF NOT EXISTS skipped INTEGER NOT NULL DEFAULT 0")
        else:
            cols = [r["name"] for r in db.execute("PRAGMA table_info(tournament_rounds)").fetchall()]
            if "skipped" not in cols:
                db.execute("ALTER TABLE tournament_rounds ADD COLUMN skipped INTEGER NOT NULL DEFAULT 0")

        if is_new_db:
            for name, _ in SEED_PLAYERS:
                db.execute(
                    "INSERT INTO players (id, name, starting_points) VALUES (?, ?, 0) "
                    "ON CONFLICT (name) DO NOTHING",
                    (str(uuid.uuid4()), name),
                )
        db.commit()
    finally:
        db.close()  # também libera o lock do Postgres


def normalize_name(s):
    s = (s or "").lower().strip()
    s = unicodedata.normalize("NFD", s)
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


def outcome(h, a):
    if h > a:
        return "H"
    if h < a:
        return "A"
    return "D"


def points_for(home_guess, away_guess, home_goals, away_goals):
    if home_goals is None or away_goals is None:
        return None
    if home_guess == home_goals and away_guess == away_goals:
        return 5
    return 3 if outcome(home_guess, away_guess) == outcome(home_goals, away_goals) else 0


MATCH_REGEX = re.compile(r"^(.+?)\s+(\d+)\s*[xX]\s*(\d+)\s+(.+)$")


def parse_round_with_guesses(text):
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines:
        return None, []
    name = lines[0]
    match_lines = lines[1:]
    if MATCH_REGEX.match(lines[0]):
        name = ""
        match_lines = lines
    matches = []
    for line in match_lines:
        m = MATCH_REGEX.match(line)
        if m:
            matches.append({
                "home": m.group(1).strip(),
                "home_guess": int(m.group(2)),
                "away_guess": int(m.group(3)),
                "away": m.group(4).strip(),
            })
    return name.strip(), matches


def extract_round_label(round_name):
    m = re.search(r"(\d+)\s*[ºªoa]?\.?\s*rodada", round_name, re.IGNORECASE)
    if m:
        return f"{m.group(1)}ª RODADA"
    m = re.search(r"rodada\s*[nº°]?\.?\s*(\d+)", round_name, re.IGNORECASE)
    if m:
        return f"{m.group(1)}ª RODADA"
    return round_name


def round_is_finished(db, round_id):
    rows = db.execute(
        "SELECT home_goals, away_goals FROM matches WHERE round_id = ?", (round_id,)
    ).fetchall()
    return bool(rows) and all(r["home_goals"] is not None and r["away_goals"] is not None for r in rows)


def player_round_points(db, round_id, player_id):
    matches = db.execute("SELECT * FROM matches WHERE round_id = ?", (round_id,)).fetchall()
    picks = db.execute(
        "SELECT * FROM picks WHERE round_id = ? AND player_id = ?", (round_id, player_id)
    ).fetchall()
    by_match = {p["match_id"]: p for p in picks}
    total = 0
    for m in matches:
        pk = by_match.get(m["id"])
        if not pk:
            continue
        pts = points_for(pk["home_guess"], pk["away_guess"], m["home_goals"], m["away_goals"])
        if pts is not None:
            total += pts
    return total


def get_tournament_rounds(db, phase=None):
    sql = (
        "SELECT tr.*, r.name AS football_name FROM tournament_rounds tr "
        "LEFT JOIN rounds r ON r.id = tr.football_round_id"
    )
    args = []
    if phase:
        sql += " WHERE tr.phase = ?"
        args.append(phase)
    sql += " ORDER BY tr.number"
    return db.execute(sql, args).fetchall()


def create_full_league_schedule(db):
    """Cria as 8 rodadas e sorteia os confrontos sem repetir adversários."""
    if get_tournament_rounds(db, "league"):
        return

    players = [r["id"] for r in db.execute("SELECT id FROM players ORDER BY name").fetchall()]
    if len(players) < 2:
        return

    random.shuffle(players)
    # Com número ímpar de participantes, um deles descansa a cada rodada (BYE).
    if len(players) % 2 == 1:
        players.append(None)
    n = len(players)
    schedule = []
    current = players[:]

    # Método do círculo: nenhum par se repete enquanto houver rodadas possíveis (n - 1).
    for _ in range(8):
        pairs = []
        for i in range(n // 2):
            a, b = current[i], current[n - 1 - i]
            if a is not None and b is not None:
                pairs.append((a, b))
        schedule.append(pairs)
        # Mantém o primeiro elemento fixo e gira os demais.
        current = [current[0], current[-1]] + current[1:-1]

    for number, pairs in enumerate(schedule, 1):
        tr_id = str(uuid.uuid4())
        db.execute(
            "INSERT INTO tournament_rounds (id, number, phase, football_round_id, created_at) VALUES (?, ?, 'league', NULL, ?)",
            (tr_id, number, datetime.utcnow().isoformat()),
        )
        for p1, p2 in pairs:
            db.execute(
                "INSERT INTO tournament_matchups (id, tournament_round_id, player1_id, player2_id) VALUES (?, ?, ?, ?)",
                (str(uuid.uuid4()), tr_id, p1, p2),
            )
    db.commit()


def league_standings(db):
    create_full_league_schedule(db)
    players = db.execute("SELECT * FROM players ORDER BY name").fetchall()
    data = {
        p["id"]: {
            "player": p, "points": 0, "played": 0, "wins": 0,
            "draws": 0, "losses": 0, "prediction_points": 0,
        }
        for p in players
    }
    for tr in get_tournament_rounds(db, "league"):
        if tr["skipped"]:
            # Rodada sem jogos: todos os jogadores ganham 3 pontos.
            for row in data.values():
                row["points"] += 3
                row["played"] += 1
            continue
        if not tr["football_round_id"] or not round_is_finished(db, tr["football_round_id"]):
            continue
        finalize_tournament_round(db, tr["id"])
        matches = db.execute(
            "SELECT * FROM tournament_matchups WHERE tournament_round_id = ?", (tr["id"],)
        ).fetchall()
        for m in matches:
            if not m["result"]:
                continue
            p1, p2 = data.get(m["player1_id"]), data.get(m["player2_id"])
            if not p1 or not p2:
                continue
            p1["played"] += 1
            p2["played"] += 1
            p1["prediction_points"] += m["player1_score"] or 0
            p2["prediction_points"] += m["player2_score"] or 0
            if m["result"] == "P1":
                p1["points"] += 3; p1["wins"] += 1; p2["losses"] += 1
            elif m["result"] == "P2":
                p2["points"] += 3; p2["wins"] += 1; p1["losses"] += 1
            else:
                p1["points"] += 1; p2["points"] += 1
                p1["draws"] += 1; p2["draws"] += 1

    ranking = list(data.values())
    ranking.sort(key=lambda r: (-r["points"], -r["wins"], -r["prediction_points"], r["player"]["name"].lower()))
    for i, row in enumerate(ranking, 1):
        row["position"] = i
    return ranking


def create_playoff_schedule(db, standings=None):
    """Cria os 4 jogos de cada série: 5x12, 6x11, 7x10 e 8x9."""
    if get_tournament_rounds(db, "playoff"):
        return
    standings = standings if standings is not None else league_standings(db)
    if len(standings) < 12:
        return
    qualified = standings[4:12]
    pairs = [(qualified[i], qualified[7 - i]) for i in range(4)]

    for number in range(1, 5):
        tr_id = str(uuid.uuid4())
        db.execute(
            "INSERT INTO tournament_rounds (id, number, phase, football_round_id, created_at) VALUES (?, ?, 'playoff', NULL, ?)",
            (tr_id, number, datetime.utcnow().isoformat()),
        )
        for p1, p2 in pairs:
            db.execute(
                "INSERT INTO tournament_matchups (id, tournament_round_id, player1_id, player2_id) VALUES (?, ?, ?, ?)",
                (str(uuid.uuid4()), tr_id, p1["player"]["id"], p2["player"]["id"]),
            )
    db.commit()


def attach_football_round(db, football_round_id):
    create_full_league_schedule(db)
    league = get_tournament_rounds(db, "league")
    for tr in league:
        if tr["football_round_id"] is None and not tr["skipped"]:
            db.execute("UPDATE tournament_rounds SET football_round_id = ? WHERE id = ?", (football_round_id, tr["id"]))
            db.commit()
            return tr["id"]

    # Só começa o playoff quando as 8 rodadas da fase de liga estiverem encerradas.
    if league and all(
        tr["skipped"] or (tr["football_round_id"] and round_is_finished(db, tr["football_round_id"]))
        for tr in league
    ):
        create_playoff_schedule(db)
        for tr in get_tournament_rounds(db, "playoff"):
            if tr["football_round_id"] is None:
                db.execute("UPDATE tournament_rounds SET football_round_id = ? WHERE id = ?", (football_round_id, tr["id"]))
                db.commit()
                return tr["id"]
    return None


def finalize_tournament_round(db, tournament_round_id):
    tr = db.execute("SELECT * FROM tournament_rounds WHERE id = ?", (tournament_round_id,)).fetchone()
    if not tr or not tr["football_round_id"] or not round_is_finished(db, tr["football_round_id"]):
        return False
    matches = db.execute("SELECT * FROM tournament_matchups WHERE tournament_round_id = ?", (tournament_round_id,)).fetchall()
    for m in matches:
        s1 = player_round_points(db, tr["football_round_id"], m["player1_id"])
        s2 = player_round_points(db, tr["football_round_id"], m["player2_id"])
        result = "P1" if s1 > s2 else ("P2" if s2 > s1 else "D")
        db.execute(
            "UPDATE tournament_matchups SET result = ?, player1_score = ?, player2_score = ? WHERE id = ?",
            (result, s1, s2, m["id"]),
        )
    db.commit()
    return True


def playoff_series(db):
    rounds = get_tournament_rounds(db, "playoff")
    if not rounds:
        return []
    first_matches = db.execute(
        "SELECT tm.*, p1.name AS p1_name, p2.name AS p2_name FROM tournament_matchups tm "
        "JOIN players p1 ON p1.id=tm.player1_id JOIN players p2 ON p2.id=tm.player2_id "
        "WHERE tm.tournament_round_id=?", (rounds[0]["id"],)
    ).fetchall()
    result = []
    for base in first_matches:
        games = []
        completed_games = 0
        p1_wins = p2_wins = 0
        for tr in rounds:
            m = db.execute(
                "SELECT * FROM tournament_matchups WHERE tournament_round_id=? AND player1_id=? AND player2_id=?",
                (tr["id"], base["player1_id"], base["player2_id"]),
            ).fetchone()
            if m:
                games.append(m)
                if m["result"]:
                    completed_games += 1
                    if m["result"] == "P1": p1_wins += 1
                    elif m["result"] == "P2": p2_wins += 1
        winner_id = None
        if completed_games == 4:
            if p1_wins > p2_wins:
                winner_id = base["player1_id"]
            elif p2_wins > p1_wins:
                winner_id = base["player2_id"]
            else:
                # Desempate: favorece o melhor colocado na liga (player1).
                winner_id = base["player1_id"]
        result.append({
            "p1_name": base["p1_name"], "p2_name": base["p2_name"],
            "p1_wins": p1_wins, "p2_wins": p2_wins,
            "games": games, "completed_games": completed_games, "winner_id": winner_id,
        })
    return result


def build_ranking_text(db):
    ranking = league_standings(db)
    lines = ["CLASSIFICAÇÃO - FASE DE LIGA"]
    for i, row in enumerate(ranking, 1):
        lines.append(f"{i}. {row['player']['name']}: {row['points']} pts")
    return "\n".join(lines)


def find_or_create_round(db, parsed_name, parsed_matches):
    team_key = ",".join(sorted(f"{normalize_name(m['home'])}|{normalize_name(m['away'])}" for m in parsed_matches))
    round_row = None
    if parsed_name:
        round_row = db.execute("SELECT * FROM rounds WHERE LOWER(name)=LOWER(?)", (parsed_name,)).fetchone()
    if round_row is None:
        for c in db.execute("SELECT * FROM rounds").fetchall():
            matches = db.execute("SELECT home, away FROM matches WHERE round_id=?", (c["id"],)).fetchall()
            key = ",".join(sorted(f"{normalize_name(m['home'])}|{normalize_name(m['away'])}" for m in matches))
            if key == team_key:
                round_row = c
                break

    created = False
    if round_row is None:
        round_id = str(uuid.uuid4())
        count = db.execute("SELECT COUNT(*) FROM rounds").fetchone()[0]
        name = parsed_name or f"Rodada {count + 1}"
        db.execute("INSERT INTO rounds (id, name, created_at) VALUES (?, ?, ?)",
                   (round_id, name, datetime.utcnow().isoformat()))
        for i, m in enumerate(parsed_matches):
            db.execute(
                "INSERT INTO matches (id, round_id, home, away, home_goals, away_goals, position) VALUES (?, ?, ?, ?, NULL, NULL, ?)",
                (str(uuid.uuid4()), round_id, m["home"], m["away"], i),
            )
        db.commit()
        round_row = db.execute("SELECT * FROM rounds WHERE id=?", (round_id,)).fetchone()
        created = True

    tournament_round_id = db.execute(
        "SELECT id FROM tournament_rounds WHERE football_round_id=?", (round_row["id"],)
    ).fetchone()
    if not tournament_round_id:
        attach_football_round(db, round_row["id"])
    return round_row, created


def save_picks(db, round_id, player_id, parsed_matches):
    matches = db.execute("SELECT * FROM matches WHERE round_id=?", (round_id,)).fetchall()
    matched = 0
    for pm in parsed_matches:
        for m in matches:
            if normalize_name(m["home"]) == normalize_name(pm["home"]) and normalize_name(m["away"]) == normalize_name(pm["away"]):
                db.execute(
                    "INSERT INTO picks (round_id, player_id, match_id, home_guess, away_guess) VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT (round_id, player_id, match_id) DO UPDATE SET "
                    "home_guess = excluded.home_guess, away_guess = excluded.away_guess",
                    (round_id, player_id, m["id"], pm["home_guess"], pm["away_guess"]),
                )
                matched += 1
                break
    db.commit()
    return matched


def canonical_team(name):
    """Normaliza nomes de clubes da Champions para comparação.

    Os nomes vêm dos jogos da própria Champions League e são comparados
    de forma tolerante a acentos e pontuação.
    """
    n = normalize_name(name)
    n = re.sub(r"[^a-z0-9 ]", "", n)
    return re.sub(r"\s+", " ", n).strip()


def teams_match(api_name, our_name):
    a, b = canonical_team(api_name), canonical_team(our_name)
    return bool(a and b and (a == b or a in b or b in a))


def fetch_results_from_football_data(matches):
    if not FOOTBALL_DATA_TOKEN:
        raise RuntimeError(
            "A variável de ambiente FOOTBALL_DATA_TOKEN não está configurada "
            f"para consultar a {COMPETITION_NAME}."
        )
    resp = requests.get(
        f"https://api.football-data.org/v4/competitions/{COMPETITION_CODE}/matches",
        headers={"X-Auth-Token": FOOTBALL_DATA_TOKEN}, timeout=20,
    )
    data = resp.json()
    if resp.status_code != 200:
        raise RuntimeError(f"A football-data.org retornou um erro: {data.get('message', data)}")
    api_matches = data.get("matches", [])
    results = []
    for m in matches:
        found = next((fx for fx in api_matches if teams_match(fx["homeTeam"]["name"], m["home"]) and teams_match(fx["awayTeam"]["name"], m["away"])), None)
        if found and found.get("status") == "FINISHED" and found.get("score", {}).get("fullTime", {}).get("home") is not None:
            results.append({"found": True, "home_goals": found["score"]["fullTime"]["home"], "away_goals": found["score"]["fullTime"]["away"]})
        else:
            results.append({"found": False})
    return results


def apply_results_to_round(db, round_id):
    matches = db.execute("SELECT * FROM matches WHERE round_id=? ORDER BY position", (round_id,)).fetchall()
    results = fetch_results_from_football_data([{"home": m["home"], "away": m["away"]} for m in matches])
    updated = 0
    for m, r in zip(matches, results):
        if r["found"]:
            db.execute("UPDATE matches SET home_goals=?, away_goals=? WHERE id=?", (r["home_goals"], r["away_goals"], m["id"]))
            updated += 1
    db.commit()
    return updated, len(matches) - updated


def get_reference_round(db):
    rows = db.execute("SELECT * FROM rounds ORDER BY created_at DESC").fetchall()
    return next((r for r in rows if round_is_finished(db, r["id"])), None)


@app.context_processor
def inject_header_stats():
    db = get_db()
    league = get_tournament_rounds(db, "league")
    filled = sum(1 for r in league if r["football_round_id"] or r["skipped"])
    finished = sum(
        1 for r in league
        if r["skipped"] or (r["football_round_id"] and round_is_finished(db, r["football_round_id"]))
    )
    return {"rounds_count": filled, "open_rounds": filled - finished, "league_finished": finished >= 8}


@app.route("/")
def index():
    return redirect(url_for("ranking"))


@app.route("/classificacao")
def ranking():
    db = get_db()
    rows = league_standings(db)
    ref = get_reference_round(db)
    copy_text = build_ranking_text(db) if rows else ""
    playoff_rows = playoff_series(db)
    return render_template("ranking.html", ranking=rows, copy_text=copy_text, active="ranking",
                           league_round_count=sum(1 for r in get_tournament_rounds(db, "league") if r["football_round_id"] or r["skipped"]),
                           playoff_pairs=rows[4:12] if len(rows) >= 12 else [], playoff_series=playoff_rows,
                           ref_round=ref)


@app.route("/rodadas")
def rodadas():
    db = get_db()
    players = db.execute("SELECT * FROM players ORDER BY name").fetchall()
    rounds_rows = db.execute("SELECT * FROM rounds ORDER BY created_at DESC").fetchall()
    rounds = []
    for r in rounds_rows:
        matches = db.execute("SELECT * FROM matches WHERE round_id=? ORDER BY position", (r["id"],)).fetchall()
        finished = round_is_finished(db, r["id"])
        picks_players = db.execute(
            "SELECT DISTINCT players.id, players.name FROM picks JOIN players ON players.id=picks.player_id WHERE picks.round_id=? ORDER BY players.name",
            (r["id"],),
        ).fetchall()
        round_scores = []
        if finished:
            for p in picks_players:
                round_scores.append({"name": p["name"], "pts": player_round_points(db, r["id"], p["id"])})
            round_scores.sort(key=lambda x: -x["pts"])
        tr = db.execute("SELECT * FROM tournament_rounds WHERE football_round_id=?", (r["id"],)).fetchone()
        rounds.append({"id": r["id"], "name": r["name"], "matches": matches, "finished": finished,
                       "picks_players": picks_players, "round_scores": round_scores, "tournament": tr})

    league_trs = get_tournament_rounds(db, "league")
    playoff_trs = get_tournament_rounds(db, "playoff")
    standings = league_standings(db)
    matchup_map = {}
    for tr in league_trs + playoff_trs:
        matchup_map[tr["id"]] = db.execute(
            "SELECT tm.*, p1.name AS p1_name, p2.name AS p2_name FROM tournament_matchups tm "
            "JOIN players p1 ON p1.id=tm.player1_id JOIN players p2 ON p2.id=tm.player2_id "
            "WHERE tm.tournament_round_id=?", (tr["id"],)
        ).fetchall()
    return render_template("rodadas.html", players=players, rounds=rounds, active="rodadas",
                           tournament_rounds=league_trs, playoff_rounds=playoff_trs,
                           matchup_map=matchup_map, standings=standings, playoff_series=playoff_series(db))


@app.route("/rodadas/palpite", methods=["POST"])
def salvar_palpite():
    db = get_db()
    player_id = request.form.get("player_id", "").strip()
    new_name = request.form.get("new_player_name", "").strip()
    text = request.form.get("pick_text", "")
    if new_name:
        existing = db.execute("SELECT * FROM players WHERE LOWER(name)=LOWER(?)", (new_name,)).fetchone()
        if existing:
            player_id = existing["id"]
        else:
            player_id = str(uuid.uuid4())
            db.execute("INSERT INTO players (id,name,starting_points) VALUES (?,?,0)", (player_id, new_name))
            db.commit()
    if not player_id:
        return redirect(url_for("rodadas"))
    parsed_name, parsed_matches = parse_round_with_guesses(text)
    if not parsed_matches:
        return redirect(url_for("rodadas"))
    round_row, _ = find_or_create_round(db, parsed_name, parsed_matches)
    tr = db.execute("SELECT * FROM tournament_rounds WHERE football_round_id=?", (round_row["id"],)).fetchone()
    # Impede criar mais de 12 rodadas de futebol dentro do torneio.
    if tr is None:
        return redirect(url_for("rodadas"))
    save_picks(db, round_row["id"], player_id, parsed_matches)
    try:
        apply_results_to_round(db, round_row["id"])
    except Exception:
        pass
    finalize_tournament_round(db, tr["id"])
    return redirect(url_for("ranking" if round_is_finished(db, round_row["id"]) else "rodadas"))


@app.route("/rodadas/torneio/<tournament_round_id>/sem-jogos", methods=["POST"])
def rodada_sem_jogos(tournament_round_id):
    """Marca uma rodada da fase de liga como "sem jogos": todos ganham 3 pontos."""
    db = get_db()
    db.execute(
        "UPDATE tournament_rounds SET skipped = 1 "
        "WHERE id = ? AND phase = 'league' AND football_round_id IS NULL",
        (tournament_round_id,),
    )
    db.commit()
    return redirect(url_for("rodadas"))


@app.route("/rodadas/torneio/<tournament_round_id>/desfazer-sem-jogos", methods=["POST"])
def desfazer_rodada_sem_jogos(tournament_round_id):
    db = get_db()
    db.execute("UPDATE tournament_rounds SET skipped = 0 WHERE id = ?", (tournament_round_id,))
    db.commit()
    return redirect(url_for("rodadas"))


@app.route("/rodadas/<round_id>/finalizar", methods=["POST"])
def finalizar_rodada(round_id):
    db = get_db()
    try:
        apply_results_to_round(db, round_id)
    except Exception:
        pass
    tr = db.execute("SELECT * FROM tournament_rounds WHERE football_round_id=?", (round_id,)).fetchone()
    if tr:
        finalize_tournament_round(db, tr["id"])
    return redirect(url_for("ranking" if round_is_finished(db, round_id) else "rodadas"))


@app.route("/rodadas/<round_id>/manual", methods=["POST"])
def salvar_manual(round_id):
    db = get_db()
    matches = db.execute("SELECT * FROM matches WHERE round_id=?", (round_id,)).fetchall()
    for m in matches:
        h = request.form.get(f"home_{m['id']}", "").strip()
        a = request.form.get(f"away_{m['id']}", "").strip()
        hg = int(h) if h.isdigit() else None
        ag = int(a) if a.isdigit() else None
        db.execute("UPDATE matches SET home_goals=?, away_goals=? WHERE id=?", (hg, ag, m["id"]))
    db.commit()
    tr = db.execute("SELECT * FROM tournament_rounds WHERE football_round_id=?", (round_id,)).fetchone()
    if tr:
        finalize_tournament_round(db, tr["id"])
    return redirect(url_for("ranking" if round_is_finished(db, round_id) else "rodadas"))


@app.route("/rodadas/<round_id>/excluir", methods=["POST"])
def excluir_rodada(round_id):
    db = get_db()
    # Mantém o confronto sorteado daquela rodada para que excluir/reimportar
    # os jogos de futebol não altere o chaveamento do torneio.
    db.execute("UPDATE tournament_rounds SET football_round_id=NULL WHERE football_round_id=?", (round_id,))
    db.execute("DELETE FROM picks WHERE round_id=?", (round_id,))
    db.execute("DELETE FROM matches WHERE round_id=?", (round_id,))
    db.execute("DELETE FROM rounds WHERE id=?", (round_id,))
    db.commit()
    return redirect(url_for("ranking"))


@app.route("/rodadas/<round_id>/remover_palpite/<player_id>", methods=["POST"])
def remover_palpite(round_id, player_id):
    db = get_db()
    db.execute("DELETE FROM picks WHERE round_id=? AND player_id=?", (round_id, player_id))
    db.commit()
    return redirect(url_for("ranking"))


@app.route("/jogadores")
def jogadores():
    db = get_db()
    players = db.execute("SELECT * FROM players ORDER BY name").fetchall()
    return render_template("jogadores.html", players=players, active="jogadores")


@app.route("/jogadores/adicionar", methods=["POST"])
def adicionar_jogador():
    db = get_db()
    name = request.form.get("name", "").strip()
    if name:
        existing = db.execute("SELECT id FROM players WHERE LOWER(name)=LOWER(?)", (name,)).fetchone()
        if not existing:
            db.execute("INSERT INTO players (id,name,starting_points) VALUES (?,?,0)", (str(uuid.uuid4()), name))
            db.commit()
    return redirect(url_for("jogadores"))


@app.route("/jogadores/<player_id>/pontos", methods=["POST"])
def ajustar_pontos(player_id):
    # Mantida apenas por compatibilidade com bancos antigos; a pontuação antiga não participa do torneio.
    return redirect(url_for("jogadores"))


@app.route("/jogadores/<player_id>/remover", methods=["POST"])
def remover_jogador(player_id):
    db = get_db()
    db.execute("DELETE FROM picks WHERE player_id=?", (player_id,))
    db.execute("DELETE FROM tournament_matchups WHERE player1_id=? OR player2_id=?", (player_id, player_id))
    db.execute("DELETE FROM players WHERE id=?", (player_id,))
    db.commit()
    return redirect(url_for("ranking"))


@app.route("/jogadores/remover-todos", methods=["POST"])
def remover_todos_jogadores():
    db = get_db()
    db.execute("DELETE FROM picks")
    db.execute("DELETE FROM tournament_matchups")
    # O sorteio da fase de liga depende dos jogadores cadastrados. Apagando as
    # rodadas do torneio, o sorteio é refeito do zero quando houver novos jogadores.
    db.execute("DELETE FROM tournament_rounds")
    db.execute("DELETE FROM players")
    db.commit()
    return redirect(url_for("jogadores"))


@app.route("/jogadores/reset", methods=["POST"])
def reset_players():
    db = get_db()
    db.execute("DELETE FROM picks")
    db.execute("DELETE FROM tournament_matchups")
    db.execute("DELETE FROM tournament_rounds")
    db.execute("DELETE FROM matches")
    db.execute("DELETE FROM rounds")
    for name, _ in SEED_PLAYERS:
        existing = db.execute("SELECT id FROM players WHERE LOWER(name)=LOWER(?)", (name,)).fetchone()
        if existing:
            db.execute("UPDATE players SET starting_points=0 WHERE id=?", (existing["id"],))
        else:
            db.execute("INSERT INTO players (id,name,starting_points) VALUES (?,?,0)", (str(uuid.uuid4()), name))
    db.commit()
    return redirect(url_for("jogadores"))


if __name__ == "__main__":
    init_db()
    app.run(debug=True, port=5000)
else:
    init_db()