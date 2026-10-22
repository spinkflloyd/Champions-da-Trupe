import os
import re
import sqlite3
import time
import unicodedata
import uuid
from datetime import datetime
import random

import requests
from flask import Flask, g, jsonify, redirect, render_template, request, send_from_directory, url_for

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_TEMPLATES_DIR = os.path.join(_BASE_DIR, "templates")
if not os.path.isdir(_TEMPLATES_DIR):
    # Layout antigo do projeto: os templates estavam em static/templates.
    _TEMPLATES_DIR = os.path.join(_BASE_DIR, "static", "templates")

app = Flask(__name__, template_folder=_TEMPLATES_DIR)

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

    def __init__(self, conn, is_pg, pool=None):
        self.conn = conn
        self.is_pg = is_pg
        self.pool = pool

    def execute(self, sql, args=()):
        if self.is_pg:
            sql = sql.replace("%", "%%").replace("?", "%s")
        return self.conn.execute(sql, tuple(args))

    def commit(self):
        self.conn.commit()

    def close(self):
        if self.pool is not None:
            try:
                self.conn.rollback()  # devolve a conexão limpa ao pool
            finally:
                self.pool.putconn(self.conn)
        else:
            self.conn.close()


_pool = None
_PG_KWARGS = {"connect_timeout": 15, "prepare_threshold": None}


def _get_pool():
    """Pool de conexões com o Postgres: evita abrir uma conexão nova (TLS) a cada página."""
    global _pool
    if _pool is None:
        try:
            from psycopg_pool import ConnectionPool
        except ImportError:
            return None  # sem o pacote, cai na conexão simples
        _pool = ConnectionPool(
            DATABASE_URL, min_size=1, max_size=5, open=True,
            check=ConnectionPool.check_connection,
            kwargs=dict(_PG_KWARGS, row_factory=_pg_row_factory),
        )
    return _pool


def connect_db():
    if DATABASE_URL:
        pool = _get_pool()
        if pool is not None:
            return DB(pool.getconn(), True, pool)
        import psycopg

        conn = psycopg.connect(DATABASE_URL, row_factory=_pg_row_factory, **_PG_KWARGS)
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
        position INTEGER NOT NULL,
        manual INTEGER NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS picks (
        round_id TEXT NOT NULL,
        player_id TEXT NOT NULL,
        match_id TEXT NOT NULL,
        home_guess INTEGER NOT NULL,
        away_guess INTEGER NOT NULL,
        PRIMARY KEY (round_id, player_id, match_id)
    )""",
    """CREATE TABLE IF NOT EXISTS round_no_shows (
        round_id TEXT NOT NULL REFERENCES rounds(id) ON DELETE CASCADE,
        player_id TEXT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
        PRIMARY KEY (round_id, player_id)
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
    "CREATE INDEX IF NOT EXISTS idx_matches_round ON matches(round_id)",
    "CREATE INDEX IF NOT EXISTS idx_matchups_round ON tournament_matchups(tournament_round_id)",
    "CREATE INDEX IF NOT EXISTS idx_trounds_football ON tournament_rounds(football_round_id)",
]


def init_db():
    db = connect_db()
    try:
        if db.is_pg:
            # Evita que dois processos criem as tabelas ao mesmo tempo.
            db.execute("SELECT pg_advisory_xact_lock(727274)")
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
            db.execute("ALTER TABLE matches ADD COLUMN IF NOT EXISTS manual INTEGER NOT NULL DEFAULT 0")
        else:
            mcols = [r["name"] for r in db.execute("PRAGMA table_info(matches)").fetchall()]
            if "manual" not in mcols:
                db.execute("ALTER TABLE matches ADD COLUMN manual INTEGER NOT NULL DEFAULT 0")
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
        db.close()


_TRANSLIT = str.maketrans({"ø": "o", "æ": "ae", "œ": "oe", "ß": "ss", "ł": "l", "đ": "d", "ð": "d", "ı": "i"})


def normalize_name(s):
    s = (s or "").lower().strip().translate(_TRANSLIT)  # "Bodø" -> "bodo"
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


def round_scoreboard(matches, picks, no_shows, players):
    """Separa os jogadores de uma rodada e calcula os pontos de quem palpitou.

    Recebe os dados já carregados (sem consultar o banco). Retorna
    (palpitaram, nao_palpitaram, sem_registro). Os pontos contam só os jogos
    que já têm resultado.
    """
    by_player = {}
    for pk in picks:
        by_player.setdefault(pk["player_id"], {})[pk["match_id"]] = pk
    scored, absent, pending = [], [], []
    for p in players:
        mine = by_player.get(p["id"])
        if mine:
            pts = 0
            for m in matches:
                pk = mine.get(m["id"])
                if pk:
                    v = points_for(pk["home_guess"], pk["away_guess"], m["home_goals"], m["away_goals"])
                    if v is not None:
                        pts += v
            scored.append({"id": p["id"], "name": p["name"], "pts": pts, "count": len(mine)})
        elif p["id"] in no_shows:
            absent.append({"id": p["id"], "name": p["name"]})
        else:
            pending.append({"id": p["id"], "name": p["name"]})
    scored.sort(key=lambda x: (-x["pts"], x["name"].lower()))
    return scored, absent, pending


def compute_scores(trs, matches, picks):
    """Pontos de cada jogador em cada rodada do torneio, calculados ao vivo.

    Retorna {tournament_round_id: {"finished": bool, "points": {player_id: pts}}}.
    """
    matches_by_round, picks_by_round = {}, {}
    for m in matches:
        matches_by_round.setdefault(m["round_id"], []).append(m)
    for pk in picks:
        picks_by_round.setdefault(pk["round_id"], []).append(pk)
    cache, scores = {}, {}
    for tr in trs:
        fid = tr["football_round_id"]
        if not fid:
            continue
        if fid not in cache:
            ms = matches_by_round.get(fid, [])
            goals = {m["id"]: (m["home_goals"], m["away_goals"]) for m in ms}
            finished = bool(ms) and all(h is not None and a is not None for h, a in goals.values())
            points = {}
            for pk in picks_by_round.get(fid, []):
                hg, ag = goals.get(pk["match_id"], (None, None))
                v = points_for(pk["home_guess"], pk["away_guess"], hg, ag)
                points[pk["player_id"]] = points.get(pk["player_id"], 0) + (v or 0)
            cache[fid] = {"finished": finished, "points": points}
        scores[tr["id"]] = cache[fid]
    return scores


def tournament_scores(db):
    """Carrega pontos de todas as rodadas do torneio em 3 consultas."""
    trs = db.execute(
        "SELECT id, football_round_id FROM tournament_rounds WHERE football_round_id IS NOT NULL"
    ).fetchall()
    if not trs:
        return {}
    fids = sorted({t["football_round_id"] for t in trs})
    marks = ",".join("?" * len(fids))
    matches = db.execute(
        f"SELECT id, round_id, home_goals, away_goals FROM matches WHERE round_id IN ({marks})", fids
    ).fetchall()
    picks = db.execute(
        f"SELECT round_id, player_id, match_id, home_guess, away_guess FROM picks WHERE round_id IN ({marks})",
        fids,
    ).fetchall()
    return compute_scores(trs, matches, picks)


def matchup_outcome(scores, tournament_round_id, player1_id, player2_id):
    """Resultado de um confronto (None enquanto a rodada não estiver encerrada)."""
    sc = scores.get(tournament_round_id)
    if not sc or not sc["finished"]:
        return None
    s1 = sc["points"].get(player1_id, 0)
    s2 = sc["points"].get(player2_id, 0)
    return {"s1": s1, "s2": s2, "result": "P1" if s1 > s2 else ("P2" if s2 > s1 else "D")}


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
    """Garante as 8 rodadas da fase de liga.

    Os confrontos de cada rodada não são sorteados aqui: cada rodada é
    sorteada separadamente, com o botão "Sortear confrontos".
    """
    if get_tournament_rounds(db, "league"):
        return
    now = datetime.utcnow().isoformat()
    for number in range(1, 9):
        db.execute(
            "INSERT INTO tournament_rounds (id, number, phase, football_round_id, created_at) "
            "VALUES (?, ?, 'league', NULL, ?) ON CONFLICT (phase, number) DO NOTHING",
            (str(uuid.uuid4()), number, now),
        )
    db.commit()


def _find_matching(players, forbidden, budget):
    """Forma pares com todos os jogadores sem usar nenhum par proibido (busca com retrocesso)."""

    def solve(remaining):
        if not remaining:
            return []
        if budget[0] <= 0:
            return None
        budget[0] -= 1

        def options(p):
            return [q for q in remaining if q != p and frozenset((p, q)) not in forbidden]

        # Começa pelo jogador com menos adversários possíveis.
        a = min(remaining, key=lambda p: (len(options(p)), random.random()))
        opts = options(a)
        random.shuffle(opts)
        for b in opts:
            rest = [x for x in remaining if x != a and x != b]
            result = solve(rest)
            if result is not None:
                return [(a, b)] + result
        return None

    return solve(list(players))


def draw_league_round(db, tournament_round_id):
    """Sorteia os confrontos de UMA rodada da fase de liga, sem repetir adversários.

    Retorna (ok, codigo). Com número ímpar de jogadores, um descansa (BYE),
    escolhido entre os que menos descansaram.
    """
    tr = db.execute(
        "SELECT * FROM tournament_rounds WHERE id = ? AND phase = 'league'", (tournament_round_id,)
    ).fetchone()
    if not tr or tr["skipped"]:
        return False, "indisponivel"
    already = db.execute(
        "SELECT COUNT(*) AS n FROM tournament_matchups WHERE tournament_round_id = ?", (tournament_round_id,)
    ).fetchone()["n"]
    if already and tr["football_round_id"]:
        return False, "bloqueada"

    players = [r["id"] for r in db.execute("SELECT id FROM players ORDER BY name").fetchall()]
    if len(players) < 2:
        return False, "sem_jogadores"

    # Pares que já se enfrentaram nas OUTRAS rodadas da liga.
    rows = db.execute(
        "SELECT m.tournament_round_id AS rid, m.player1_id AS p1, m.player2_id AS p2 "
        "FROM tournament_matchups m JOIN tournament_rounds r ON r.id = m.tournament_round_id "
        "WHERE r.phase = 'league' AND r.skipped = 0 AND r.id <> ?",
        (tournament_round_id,),
    ).fetchall()
    forbidden = set()
    playing = {}
    for r in rows:
        forbidden.add(frozenset((r["p1"], r["p2"])))
        playing.setdefault(r["rid"], set()).update((r["p1"], r["p2"]))
    rests = {p: sum(1 for who in playing.values() if p not in who) for p in players}

    budget = [15000]
    pairs = None
    if len(players) % 2 == 0:
        while not pairs and budget[0] > 0:
            pairs = _find_matching(players, forbidden, budget)
            if pairs is None:
                break
    else:
        for bye in sorted(players, key=lambda p: (rests[p], random.random())):
            pairs = _find_matching([p for p in players if p != bye], forbidden, budget)
            if pairs or budget[0] <= 0:
                break
    if not pairs:
        return False, "esgotado"

    db.execute("DELETE FROM tournament_matchups WHERE tournament_round_id = ?", (tournament_round_id,))
    for a, b in pairs:
        if random.random() < 0.5:
            a, b = b, a
        db.execute(
            "INSERT INTO tournament_matchups (id, tournament_round_id, player1_id, player2_id) VALUES (?, ?, ?, ?)",
            (str(uuid.uuid4()), tournament_round_id, a, b),
        )
    db.commit()
    return True, "ok"


def league_standings(db, scores=None):
    rounds = get_tournament_rounds(db, "league")
    if not rounds:
        create_full_league_schedule(db)
        rounds = get_tournament_rounds(db, "league")
    if scores is None:
        scores = tournament_scores(db)
    players = db.execute("SELECT * FROM players ORDER BY name").fetchall()
    data = {
        p["id"]: {
            "player": p, "points": 0, "played": 0, "wins": 0,
            "draws": 0, "losses": 0, "prediction_points": 0,
        }
        for p in players
    }
    by_round = {}
    for m in db.execute("SELECT tournament_round_id, player1_id, player2_id FROM tournament_matchups").fetchall():
        by_round.setdefault(m["tournament_round_id"], []).append(m)

    for tr in rounds:
        if tr["skipped"]:
            # Rodada sem jogos: todos os jogadores ganham 3 pontos.
            for row in data.values():
                row["points"] += 3
                row["played"] += 1
            continue
        for m in by_round.get(tr["id"], []):
            out = matchup_outcome(scores, tr["id"], m["player1_id"], m["player2_id"])
            if out is None:
                continue
            p1, p2 = data.get(m["player1_id"]), data.get(m["player2_id"])
            if not p1 or not p2:
                continue
            p1["played"] += 1
            p2["played"] += 1
            p1["prediction_points"] += out["s1"]
            p2["prediction_points"] += out["s2"]
            if out["result"] == "P1":
                p1["points"] += 3; p1["wins"] += 1; p2["losses"] += 1
            elif out["result"] == "P2":
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
            # Se esqueceram de sortear esta rodada, sorteia agora.
            has = db.execute(
                "SELECT COUNT(*) AS n FROM tournament_matchups WHERE tournament_round_id = ?", (tr["id"],)
            ).fetchone()["n"]
            if not has:
                draw_league_round(db, tr["id"])
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
    """Mantida por compatibilidade: os resultados dos confrontos agora são
    calculados ao vivo a partir dos palpites, então não há nada a gravar."""
    return True


def playoff_series(db, scores=None):
    rounds = get_tournament_rounds(db, "playoff")
    if not rounds:
        return []
    if scores is None:
        scores = tournament_scores(db)
    ids = [tr["id"] for tr in rounds]
    marks = ",".join("?" * len(ids))
    rows = db.execute(
        "SELECT tm.*, p1.name AS p1_name, p2.name AS p2_name FROM tournament_matchups tm "
        "JOIN players p1 ON p1.id=tm.player1_id JOIN players p2 ON p2.id=tm.player2_id "
        f"WHERE tm.tournament_round_id IN ({marks})", ids
    ).fetchall()
    by_round = {}
    for r in rows:
        by_round.setdefault(r["tournament_round_id"], []).append(r)
    result = []
    for base in by_round.get(rounds[0]["id"], []):
        games = []
        completed_games = 0
        p1_wins = p2_wins = 0
        for tr in rounds:
            m = next((x for x in by_round.get(tr["id"], [])
                      if x["player1_id"] == base["player1_id"] and x["player2_id"] == base["player2_id"]), None)
            if m:
                games.append(m)
                out = matchup_outcome(scores, tr["id"], m["player1_id"], m["player2_id"])
                if out:
                    completed_games += 1
                    if out["result"] == "P1": p1_wins += 1
                    elif out["result"] == "P2": p2_wins += 1
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


def share_round_options(db, scores=None):
    """Rodadas da liga que podem aparecer no texto de compartilhar (sorteadas ou sem jogos)."""
    if scores is None:
        scores = tournament_scores(db)
    drawn = {r["rid"] for r in db.execute(
        "SELECT DISTINCT tournament_round_id AS rid FROM tournament_matchups").fetchall()}
    options = []
    for tr in get_tournament_rounds(db, "league"):
        if tr["skipped"] or tr["id"] in drawn:
            finished = bool(tr["skipped"]) or bool(scores.get(tr["id"], {}).get("finished"))
            options.append({"number": tr["number"], "finished": finished, "skipped": bool(tr["skipped"])})
    return options


def default_share_round(options):
    """Por padrão, a última rodada encerrada (a que você acabou de finalizar)."""
    done = [o["number"] for o in options if o["finished"] and not o["skipped"]]
    return max(done) if done else None


def build_ranking_text(db, round_number=None, ranking=None, scores=None):
    """Texto para colar no WhatsApp: classificação + confrontos de UMA rodada, com os resultados."""
    if scores is None:
        scores = tournament_scores(db)
    if ranking is None:
        ranking = league_standings(db, scores)
    options = share_round_options(db, scores)
    if round_number not in {o["number"] for o in options}:
        round_number = default_share_round(options)
    lines = ["CLASSIFICAÇÃO - FASE DE LIGA"]
    for i, row in enumerate(ranking, 1):
        lines.append(f"{i}. {row['player']['name']}: {row['points']} pts")

    all_names = {p["id"]: p["name"] for p in db.execute("SELECT id, name FROM players").fetchall()}
    chosen = next((tr for tr in get_tournament_rounds(db, "league") if tr["number"] == round_number), None)
    block = None
    if chosen is not None:
        if chosen["skipped"]:
            block = [f"Rodada {chosen['number']}: sem jogos (todos ganharam 3 pts)"]
        else:
            ms = db.execute(
                "SELECT player1_id AS p1, player2_id AS p2 FROM tournament_matchups WHERE tournament_round_id = ?",
                (chosen["id"],),
            ).fetchall()
            if ms:
                finished = bool(scores.get(chosen["id"], {}).get("finished"))
                block = [f"Rodada {chosen['number']}" + ("" if finished else " (aguardando resultados)")]
                playing = set()
                for m in sorted(ms, key=lambda x: all_names.get(x["p1"], "").lower()):
                    n1, n2 = all_names.get(m["p1"], "?"), all_names.get(m["p2"], "?")
                    playing.update((m["p1"], m["p2"]))
                    out = matchup_outcome(scores, chosen["id"], m["p1"], m["p2"])
                    block.append(f"{n1} {out['s1']} x {out['s2']} {n2}" if out else f"{n1} x {n2}")
                resting = sorted(all_names[pid] for pid in all_names if pid not in playing)
                if resting:
                    block.append("Folga: " + ", ".join(resting))
    if block:
        lines.append("")
        lines.append("CONFRONTOS")
        lines.append("")
        lines.extend(block)

    series = playoff_series(db, scores)
    if series:
        lines.append("")
        lines.append("PLAYOFF (melhor de 4 jogos)")
        for sr in series:
            lines.append(f"{sr['p1_name']} {sr['p1_wins']} x {sr['p2_wins']} {sr['p2_name']} ({sr['completed_games']}/4 jogos)")
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
    db.execute("DELETE FROM round_no_shows WHERE round_id=? AND player_id=?", (round_id, player_id))
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


def team_matches(team, our_name):
    """Compara um time da API (name, shortName, tla) com o nome escrito no palpite.

    Aceita nomes abreviados como "PSG" ou "Bodo Glimt" (sem o "/" do nome oficial).
    A sigla (tla) só vale se for idêntica, para não confundir clubes parecidos.
    """
    b = canonical_team(our_name)
    if not b:
        return False
    if canonical_team(team.get("tla")) == b:
        return True
    for cand in (team.get("name"), team.get("shortName")):
        a = canonical_team(cand)
        if not a:
            continue
        if a == b or a in b or b in a:
            return True
        a2, b2 = a.replace(" ", ""), b.replace(" ", "")
        if a2 == b2 or a2 in b2 or b2 in a2:
            return True
    return False


_API_CACHE = {"time": 0.0, "matches": None}
API_CACHE_SECONDS = 60


def get_api_matches(fresh=False):
    """Todos os jogos da Champions na football-data.org. Fica 60 s em memória para não
    repetir a chamada (o plano gratuito tem limite de requisições)."""
    if not FOOTBALL_DATA_TOKEN:
        raise RuntimeError(
            "A variável de ambiente FOOTBALL_DATA_TOKEN não está configurada "
            f"para consultar a {COMPETITION_NAME}."
        )
    now = time.time()
    if not fresh and _API_CACHE["matches"] is not None and now - _API_CACHE["time"] < API_CACHE_SECONDS:
        return _API_CACHE["matches"]
    resp = requests.get(
        f"https://api.football-data.org/v4/competitions/{COMPETITION_CODE}/matches",
        headers={"X-Auth-Token": FOOTBALL_DATA_TOKEN}, timeout=10,
    )
    data = resp.json()
    if resp.status_code != 200:
        raise RuntimeError(f"A football-data.org retornou um erro: {data.get('message', data)}")
    api_matches = data.get("matches", [])
    _API_CACHE["time"], _API_CACHE["matches"] = now, api_matches
    return api_matches


def fetch_results_from_football_data(matches, fresh=False):
    api_matches = get_api_matches(fresh=fresh)
    results = []
    for m in matches:
        found = next((fx for fx in api_matches
                      if team_matches(fx["homeTeam"], m["home"]) and team_matches(fx["awayTeam"], m["away"])), None)
        if found and found.get("status") == "FINISHED" and found.get("score", {}).get("fullTime", {}).get("home") is not None:
            results.append({"found": True, "home_goals": found["score"]["fullTime"]["home"], "away_goals": found["score"]["fullTime"]["away"]})
        else:
            results.append({"found": False})
    return results


# Como o grupo escreve o nome de cada clube (trecho do nome oficial -> nome usado).
TEAM_DISPLAY = [
    ("saintgermain", "PSG"), ("manchester city", "Manchester City"), ("manchester united", "Manchester United"),
    ("bayern", "Bayern"), ("atletico", "Atlético de Madrid"), ("bodo", "Bodo Glimt"), ("betis", "Betis"),
    ("brugge", "Brugge"), ("internazionale", "Inter"), ("sporting", "Sporting"), ("leipzig", "Leipzig"),
    ("slavia", "Slavia Praha"), ("dortmund", "Borussia Dortmund"), ("aek", "AEK"), ("fenerbahce", "Fenerbahçe"),
    ("shakhtar", "Shakhtar Donetsk"), ("slovan", "Slovan Bratislava"), ("psv", "PSV"), ("porto", "Porto"),
]
_NAME_NOISE = {"fc", "cf", "sk", "fk", "kv", "osc", "ssc", "ac", "as", "afc", "sc", "club", "fa", "ks", "bk", "rc"}


def display_team_name(team):
    """Nome do time no estilo do grupo (ex.: "PSG", "Bodo Glimt", "Atlético de Madrid")."""
    blob = f"{canonical_team(team.get('name'))} {canonical_team(team.get('shortName'))}"
    for key, shown in TEAM_DISPLAY:
        if key in blob:
            return shown
    base = (team.get("shortName") or team.get("name") or "").strip()
    words = [w for w in base.split() if w.lower().strip(".") not in _NAME_NOISE and not w.isdigit()]
    return " ".join(words) or base


NOT_STARTED = {"SCHEDULED", "TIMED"}
STAGE_LABELS = {
    "PLAYOFFS": "PLAYOFFS", "LAST_16": "OITAVAS DE FINAL", "QUARTER_FINALS": "QUARTAS DE FINAL",
    "SEMI_FINALS": "SEMIFINAL", "FINAL": "FINAL",
}


def _round_key(fx):
    """Agrupa os jogos por rodada (fase + número; sem número, pela semana do jogo)."""
    md = fx.get("matchday")
    if md is None:
        try:
            d = datetime.strptime((fx.get("utcDate") or "")[:10], "%Y-%m-%d")
            md = "w%d-%02d" % d.isocalendar()[:2]
        except ValueError:
            md = "?"
    return (fx.get("stage"), md)


def build_next_round_text(requested=None, fresh=True):
    """Monta o texto dos jogos de uma rodada no formato do WhatsApp.

    Sem `requested`, usa a próxima rodada que ainda não começou. Com um número,
    usa essa rodada da fase de liga. Retorna {"texto": str|None, "erro": str|None}.
    """
    try:
        api_matches = get_api_matches(fresh=fresh)
    except RuntimeError as exc:
        return {"texto": None, "erro": str(exc)}
    except requests.RequestException:
        return {"texto": None, "erro": "Não consegui falar com a football-data.org agora. Tente de novo em instantes."}

    groups = {}
    for fx in api_matches:
        groups.setdefault(_round_key(fx), []).append(fx)
    ordered = sorted(groups.items(), key=lambda kv: min((m.get("utcDate") or "9999") for m in kv[1]))

    chosen = None
    for (stage, md), ms in ordered:
        if requested:
            if stage == "LEAGUE_STAGE" and md == requested:
                chosen = (stage, md, ms)
                break
        elif all(m.get("status") in NOT_STARTED for m in ms):
            chosen = (stage, md, ms)
            break
    if chosen is None:
        msg = "Não encontrei essa rodada na football-data.org." if requested else \
            "Não encontrei uma próxima rodada que ainda não tenha começado."
        return {"texto": None, "erro": msg}

    stage, md, ms = chosen
    games = [m for m in ms if (m.get("homeTeam") or {}).get("name") and (m.get("awayTeam") or {}).get("name")]
    if not games:
        return {"texto": None, "erro": "Os confrontos desta fase ainda não foram definidos."}
    games.sort(key=lambda m: m.get("utcDate") or "")  # estável: mantém a ordem da API dentro do mesmo horário
    if stage == "LEAGUE_STAGE":
        title = f"{COMPETITION_NAME.upper()} - {md}° RODADA"
    else:
        title = f"{COMPETITION_NAME.upper()} - {STAGE_LABELS.get(stage, (stage or '').replace('_', ' '))}"
    lines = [title] + [f"{display_team_name(m['homeTeam'])} x {display_team_name(m['awayTeam'])}" for m in games]
    return {"texto": "\n".join(lines), "erro": None}


def apply_results_to_round(db, round_id, only_missing=False, fresh=False):
    """Busca os resultados e grava nos jogos.

    only_missing=True: só consulta a API se algum jogo ainda estiver sem resultado
    (usado ao salvar palpites, para não chamar a API à toa).
    """
    matches = db.execute("SELECT * FROM matches WHERE round_id=? ORDER BY position", (round_id,)).fetchall()
    if only_missing and not any(
        m["home_goals"] is None or m["away_goals"] is None for m in matches if not m["manual"]
    ):
        return 0, 0
    results = fetch_results_from_football_data([{"home": m["home"], "away": m["away"]} for m in matches], fresh=fresh)
    updated = 0
    for m, r in zip(matches, results):
        if m["manual"]:
            continue  # resultado preenchido/corrigido à mão: a busca automática não mexe
        if r["found"]:
            db.execute("UPDATE matches SET home_goals=?, away_goals=? WHERE id=?", (r["home_goals"], r["away_goals"], m["id"]))
            updated += 1
    db.commit()
    return updated, len(matches) - updated


@app.context_processor
def inject_header_stats():
    db = get_db()
    rows = db.execute(
        "SELECT tr.skipped AS skipped, tr.football_round_id AS fid, "
        "(SELECT COUNT(*) FROM matches m WHERE m.round_id = tr.football_round_id) AS total, "
        "(SELECT COUNT(*) FROM matches m WHERE m.round_id = tr.football_round_id "
        " AND m.home_goals IS NOT NULL AND m.away_goals IS NOT NULL) AS done "
        "FROM tournament_rounds tr WHERE tr.phase = 'league'"
    ).fetchall()
    filled = sum(1 for r in rows if r["fid"] or r["skipped"])
    finished = sum(1 for r in rows if r["skipped"] or (r["fid"] and r["total"] > 0 and r["total"] == r["done"]))
    return {"rounds_count": filled, "open_rounds": filled - finished, "league_finished": finished >= 8}


@app.route("/")
def index():
    return redirect(url_for("ranking"))


@app.route("/healthz")
def healthz():
    """Ping leve usado por serviços de keep-alive (cron-job.org, UptimeRobot...).

    Consulta o banco de propósito: assim o Postgres do Neon também não hiberna.
    """
    try:
        get_db().execute("SELECT 1").fetchone()
        db_ok = True
    except Exception:
        db_ok = False
    resp = jsonify(status="ok", db=db_ok)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/manifest.webmanifest")
def manifest():
    resp = send_from_directory(
        app.static_folder, "manifest.webmanifest", mimetype="application/manifest+json"
    )
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/sw.js")
def service_worker():
    """O service worker precisa ficar na raiz do site para controlar todas as páginas."""
    resp = send_from_directory(app.static_folder, "sw.js", mimetype="application/javascript")
    resp.headers["Service-Worker-Allowed"] = "/"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/classificacao")
def ranking():
    db = get_db()
    scores = tournament_scores(db)
    rows = league_standings(db, scores)
    share_rounds = share_round_options(db, scores)
    requested = request.args.get("rodada", type=int)
    copy_round = requested if requested in {o["number"] for o in share_rounds} else default_share_round(share_rounds)
    copy_text = build_ranking_text(db, copy_round, rows, scores) if rows else ""
    playoff_rows = playoff_series(db, scores)
    league_round_count = sum(
        1 for r in get_tournament_rounds(db, "league") if r["football_round_id"] or r["skipped"]
    )
    return render_template("ranking.html", ranking=rows, copy_text=copy_text, active="ranking",
                           share_rounds=share_rounds, copy_round=copy_round,
                           league_round_count=league_round_count,
                           playoff_pairs=rows[4:12] if len(rows) >= 12 else [], playoff_series=playoff_rows)


SORTEIO_MENSAGENS = {
    "ok": (True, "Confrontos da rodada {n} sorteados."),
    "sem_jogadores": (False, "Cadastre pelo menos 2 jogadores antes de sortear."),
    "esgotado": (False, "Não foi possível sortear a rodada {n} sem repetir adversários. "
                        "Com poucos jogadores isso é esperado; cadastre mais jogadores ou use "
                        '"Sortear novamente" em uma rodada anterior.'),
    "bloqueada": (False, "A rodada {n} já recebeu jogos e não pode ser sorteada de novo."),
    "indisponivel": (False, "Esta rodada não pode ser sorteada."),
}


@app.route("/rodadas")
def rodadas():
    db = get_db()
    create_full_league_schedule(db)
    players = db.execute("SELECT * FROM players ORDER BY name").fetchall()
    rounds_rows = db.execute("SELECT * FROM rounds ORDER BY created_at DESC").fetchall()
    trs_all = get_tournament_rounds(db)
    tr_by_football = {t["football_round_id"]: t for t in trs_all if t["football_round_id"]}

    # Carrega tudo de uma vez (poucas consultas), em vez de uma por rodada/jogador.
    matches_all, picks_all, no_shows_all = [], [], []
    if rounds_rows:
        marks = ",".join("?" * len(rounds_rows))
        rids = [r["id"] for r in rounds_rows]
        matches_all = db.execute(
            f"SELECT * FROM matches WHERE round_id IN ({marks}) ORDER BY position", rids).fetchall()
        picks_all = db.execute(
            f"SELECT round_id, player_id, match_id, home_guess, away_guess FROM picks WHERE round_id IN ({marks})",
            rids).fetchall()
        no_shows_all = db.execute(
            f"SELECT round_id, player_id FROM round_no_shows WHERE round_id IN ({marks})", rids).fetchall()
    matches_by, picks_by, shows_by = {}, {}, {}
    for m in matches_all:
        matches_by.setdefault(m["round_id"], []).append(m)
    for pk in picks_all:
        picks_by.setdefault(pk["round_id"], []).append(pk)
    for ns in no_shows_all:
        shows_by.setdefault(ns["round_id"], set()).add(ns["player_id"])
    scores = compute_scores(trs_all, matches_all, picks_all)

    rounds = []
    for r in rounds_rows:
        matches = matches_by.get(r["id"], [])
        with_result = sum(1 for m in matches if m["home_goals"] is not None and m["away_goals"] is not None)
        finished = bool(matches) and with_result == len(matches)
        scored, absent, pending = round_scoreboard(
            matches, picks_by.get(r["id"], []), shows_by.get(r["id"], set()), players)
        rounds.append({"id": r["id"], "name": r["name"], "matches": matches, "finished": finished,
                       "scored": scored, "absent": absent, "pending": pending,
                       "total_matches": len(matches), "with_result": with_result,
                       "tournament": tr_by_football.get(r["id"])})

    # Aviso logo após salvar um palpite: mostra os pontos que o jogador fez.
    saved = None
    sp, sr = request.args.get("salvo_jogador"), request.args.get("salvo_rodada")
    if sp and sr:
        rd = next((x for x in rounds if x["id"] == sr), None)
        entry = next((x for x in rd["scored"] if x["id"] == sp), None) if rd else None
        if entry:
            saved = {"name": entry["name"], "round": rd["name"], "pts": entry["pts"], "count": entry["count"],
                     "total": rd["total_matches"], "with_result": rd["with_result"]}

    league_trs = [t for t in trs_all if t["phase"] == "league"]
    playoff_trs = [t for t in trs_all if t["phase"] == "playoff"]

    # Resumo logo após salvar resultados à mão.
    resultados = None
    rr = request.args.get("resultados")
    if rr:
        rd = next((x for x in rounds if x["id"] == rr), None)
        if rd:
            resultados = {
                "round": rd["name"], "with_result": rd["with_result"], "total": rd["total_matches"],
                "missing": [f"{m['home']} x {m['away']}" for m in rd["matches"]
                            if m["home_goals"] is None or m["away_goals"] is None],
            }
    not_found_games = request.args.getlist("nj")[:10]

    # Jogos da próxima rodada (só consulta a API quando o botão é clicado).
    proxima = None
    pedido = request.args.get("proxima")
    if pedido:
        proxima = build_next_round_text(int(pedido) if pedido.isdigit() else None)

    sorteio_msg, sorteio_ok = None, False
    code = request.args.get("sorteio")
    n = request.args.get("rodada", type=int)
    if code in SORTEIO_MENSAGENS:
        sorteio_ok, text = SORTEIO_MENSAGENS[code]
        sorteio_msg = text.format(n=n if n else "")

    matchup_map = {t["id"]: [] for t in trs_all}
    for m in db.execute(
        "SELECT tm.*, p1.name AS p1_name, p2.name AS p2_name FROM tournament_matchups tm "
        "JOIN players p1 ON p1.id=tm.player1_id JOIN players p2 ON p2.id=tm.player2_id"
    ).fetchall():
        d = dict(m)
        out = matchup_outcome(scores, m["tournament_round_id"], m["player1_id"], m["player2_id"])
        d["result"] = out["result"] if out else None
        d["player1_score"] = out["s1"] if out else None
        d["player2_score"] = out["s2"] if out else None
        matchup_map.setdefault(m["tournament_round_id"], []).append(d)
    for lst in matchup_map.values():
        lst.sort(key=lambda x: x["p1_name"].lower())
    return render_template("rodadas.html", players=players, rounds=rounds, active="rodadas",
                           tournament_rounds=league_trs, playoff_rounds=playoff_trs,
                           matchup_map=matchup_map, playoff_series=playoff_series(db, scores),
                           sorteio_msg=sorteio_msg, sorteio_ok=sorteio_ok, saved=saved,
                           resultados=resultados, not_found_games=not_found_games,
                           proxima=proxima, proxima_pedido=pedido or "")


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
    round_matches = db.execute("SELECT home, away FROM matches WHERE round_id=?", (round_row["id"],)).fetchall()
    known = {(normalize_name(m["home"]), normalize_name(m["away"])) for m in round_matches}
    not_found = [
        f"{pm['home']} x {pm['away']}"[:80]
        for pm in parsed_matches
        if (normalize_name(pm["home"]), normalize_name(pm["away"])) not in known
    ][:10]
    try:
        apply_results_to_round(db, round_row["id"], only_missing=True)
    except Exception:
        pass
    finalize_tournament_round(db, tr["id"])
    return redirect(url_for("rodadas", salvo_jogador=player_id, salvo_rodada=round_row["id"], nj=not_found,
                            _anchor=f"round-{round_row['id']}"))


@app.route("/rodadas/torneio/<tournament_round_id>/sem-jogos", methods=["POST"])
def rodada_sem_jogos(tournament_round_id):
    """Marca uma rodada da fase de liga como "sem jogos": todos ganham 3 pontos."""
    db = get_db()
    tr = db.execute(
        "SELECT id FROM tournament_rounds WHERE id = ? AND phase = 'league' AND football_round_id IS NULL",
        (tournament_round_id,),
    ).fetchone()
    if tr:
        db.execute("UPDATE tournament_rounds SET skipped = 1 WHERE id = ?", (tournament_round_id,))
        # Uma rodada sem jogos não tem confrontos; os sorteados deixam de valer.
        db.execute("DELETE FROM tournament_matchups WHERE tournament_round_id = ?", (tournament_round_id,))
        db.commit()
    return redirect(url_for("rodadas"))


@app.route("/rodadas/torneio/<tournament_round_id>/desfazer-sem-jogos", methods=["POST"])
def desfazer_rodada_sem_jogos(tournament_round_id):
    db = get_db()
    db.execute("UPDATE tournament_rounds SET skipped = 0 WHERE id = ?", (tournament_round_id,))
    db.commit()
    return redirect(url_for("rodadas"))


@app.route("/rodadas/torneio/<tournament_round_id>/sortear", methods=["POST"])
def sortear_rodada(tournament_round_id):
    db = get_db()
    ok, code = draw_league_round(db, tournament_round_id)
    tr = db.execute("SELECT number FROM tournament_rounds WHERE id = ?", (tournament_round_id,)).fetchone()
    return redirect(url_for("rodadas", sorteio=code, rodada=tr["number"] if tr else 0))


@app.route("/rodadas/<round_id>/finalizar", methods=["POST"])
def finalizar_rodada(round_id):
    db = get_db()
    try:
        apply_results_to_round(db, round_id, fresh=True)
    except Exception:
        pass
    tr = db.execute("SELECT * FROM tournament_rounds WHERE football_round_id=?", (round_id,)).fetchone()
    if tr:
        finalize_tournament_round(db, tr["id"])
    if round_is_finished(db, round_id):
        if tr and tr["phase"] == "league":
            return redirect(url_for("ranking", rodada=tr["number"], _anchor="compartilhar"))
        return redirect(url_for("ranking"))
    return redirect(url_for("rodadas"))


@app.route("/rodadas/<round_id>/manual", methods=["POST"])
def salvar_manual(round_id):
    db = get_db()
    matches = db.execute("SELECT * FROM matches WHERE round_id=?", (round_id,)).fetchall()
    for m in matches:
        h = request.form.get(f"home_{m['id']}", "").strip()
        a = request.form.get(f"away_{m['id']}", "").strip()
        hg = int(h) if h.isascii() and h.isdigit() else None
        ag = int(a) if a.isascii() and a.isdigit() else None
        if hg is not None and ag is not None:
            # Tudo o que é salvo aqui fica "manual": a busca automática não desfaz.
            manual = 1
        else:
            hg = ag = None
            manual = 0  # campo vazio: volta a aceitar o resultado da busca automática
        db.execute(
            "UPDATE matches SET home_goals=?, away_goals=?, manual=? WHERE id=?",
            (hg, ag, manual, m["id"]),
        )
    db.commit()
    tr = db.execute("SELECT * FROM tournament_rounds WHERE football_round_id=?", (round_id,)).fetchone()
    if tr:
        finalize_tournament_round(db, tr["id"])
    return redirect(url_for("rodadas", resultados=round_id, _anchor=f"round-{round_id}"))


@app.route("/rodadas/<round_id>/jogos/adicionar", methods=["POST"])
def adicionar_jogo(round_id):
    """Acrescenta à rodada um jogo que ficou de fora do primeiro palpite colado."""
    db = get_db()
    home = request.form.get("home", "").strip()
    away = request.form.get("away", "").strip()
    if home and away and db.execute("SELECT 1 FROM rounds WHERE id=?", (round_id,)).fetchone():
        existing = db.execute("SELECT home, away, position FROM matches WHERE round_id=?", (round_id,)).fetchall()
        dup = any(
            normalize_name(m["home"]) == normalize_name(home) and normalize_name(m["away"]) == normalize_name(away)
            for m in existing
        )
        if not dup:
            pos = max([m["position"] for m in existing], default=-1) + 1
            db.execute(
                "INSERT INTO matches (id, round_id, home, away, home_goals, away_goals, position) "
                "VALUES (?, ?, ?, ?, NULL, NULL, ?)",
                (str(uuid.uuid4()), round_id, home, away, pos),
            )
            db.commit()
    return redirect(url_for("rodadas", _anchor=f"round-{round_id}"))


@app.route("/rodadas/<round_id>/jogos/<match_id>/remover", methods=["POST"])
def remover_jogo(round_id, match_id):
    """Remove um jogo da rodada (e os palpites feitos nele), por exemplo um time digitado errado."""
    db = get_db()
    if db.execute("SELECT 1 FROM matches WHERE id=? AND round_id=?", (match_id, round_id)).fetchone():
        db.execute("DELETE FROM picks WHERE match_id=?", (match_id,))
        db.execute("DELETE FROM matches WHERE id=?", (match_id,))
        db.commit()
        tr = db.execute("SELECT * FROM tournament_rounds WHERE football_round_id=?", (round_id,)).fetchone()
        if tr:
            finalize_tournament_round(db, tr["id"])
    return redirect(url_for("rodadas", _anchor=f"round-{round_id}"))


@app.route("/rodadas/<round_id>/excluir", methods=["POST"])
def excluir_rodada(round_id):
    db = get_db()
    # Mantém o confronto sorteado daquela rodada para que excluir/reimportar
    # os jogos de futebol não altere o chaveamento do torneio.
    db.execute("UPDATE tournament_rounds SET football_round_id=NULL WHERE football_round_id=?", (round_id,))
    db.execute("DELETE FROM picks WHERE round_id=?", (round_id,))
    db.execute("DELETE FROM round_no_shows WHERE round_id=?", (round_id,))
    db.execute("DELETE FROM matches WHERE round_id=?", (round_id,))
    db.execute("DELETE FROM rounds WHERE id=?", (round_id,))
    db.commit()
    return redirect(url_for("ranking"))


@app.route("/rodadas/<round_id>/nao-palpitou/<player_id>", methods=["POST"])
def nao_palpitou(round_id, player_id):
    """Registra que o jogador não palpitou na rodada: ele fica com 0 pontos."""
    db = get_db()
    has_picks = db.execute(
        "SELECT 1 FROM picks WHERE round_id=? AND player_id=?", (round_id, player_id)
    ).fetchone()
    exists = db.execute("SELECT 1 FROM rounds WHERE id=?", (round_id,)).fetchone() and \
        db.execute("SELECT 1 FROM players WHERE id=?", (player_id,)).fetchone()
    if exists and not has_picks:
        db.execute(
            "INSERT INTO round_no_shows (round_id, player_id) VALUES (?, ?) ON CONFLICT (round_id, player_id) DO NOTHING",
            (round_id, player_id),
        )
        db.commit()
    return redirect(url_for("rodadas", _anchor=f"round-{round_id}"))


@app.route("/rodadas/<round_id>/desfazer-nao-palpitou/<player_id>", methods=["POST"])
def desfazer_nao_palpitou(round_id, player_id):
    db = get_db()
    db.execute("DELETE FROM round_no_shows WHERE round_id=? AND player_id=?", (round_id, player_id))
    db.commit()
    return redirect(url_for("rodadas", _anchor=f"round-{round_id}"))


@app.route("/rodadas/<round_id>/nao-palpitou-todos", methods=["POST"])
def nao_palpitou_todos(round_id):
    """Marca como "não palpitou" todos os jogadores que ainda não têm palpite na rodada."""
    db = get_db()
    if db.execute("SELECT 1 FROM rounds WHERE id=?", (round_id,)).fetchone():
        players = db.execute("SELECT id FROM players").fetchall()
        with_picks = {r["player_id"] for r in db.execute(
            "SELECT DISTINCT player_id FROM picks WHERE round_id=?", (round_id,)).fetchall()}
        for p in players:
            if p["id"] not in with_picks:
                db.execute(
                    "INSERT INTO round_no_shows (round_id, player_id) VALUES (?, ?) "
                    "ON CONFLICT (round_id, player_id) DO NOTHING",
                    (round_id, p["id"]),
                )
        db.commit()
    return redirect(url_for("rodadas", _anchor=f"round-{round_id}"))


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
    db.execute("DELETE FROM round_no_shows WHERE player_id=?", (player_id,))
    db.execute("DELETE FROM tournament_matchups WHERE player1_id=? OR player2_id=?", (player_id, player_id))
    db.execute("DELETE FROM players WHERE id=?", (player_id,))
    db.commit()
    return redirect(url_for("ranking"))


@app.route("/jogadores/remover-todos", methods=["POST"])
def remover_todos_jogadores():
    db = get_db()
    db.execute("DELETE FROM picks")
    db.execute("DELETE FROM round_no_shows")
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
    db.execute("DELETE FROM round_no_shows")
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