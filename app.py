"""
DBConverter — backend Flask (opcional).

O projeto roda completo pelo Live Server do VS Code, sem Python. Este backend
é a alternativa "servidor": usuários e histórico ficam num banco de dados
(SQLite local por padrão, ou PostgreSQL via DATABASE_URL) em vez do navegador.

A interface é a mesma (index.html); o Flask apenas a marca com
window.DBC_BACKEND = 'flask' para que ela use esta API.
"""
import ast
import json
import os
import re
import secrets
import sqlite3
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from functools import wraps

from flask import Flask, Response, jsonify, request, session
from werkzeug.security import check_password_hash, generate_password_hash

import converter

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # psycopg só é necessário com PostgreSQL
    psycopg = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
SQLITE_PATH = os.getenv("SQLITE_PATH", os.path.join(DATA_DIR, "app.db"))
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = "postgresql://" + DATABASE_URL[len("postgres://"):]
USE_PG = bool(DATABASE_URL)

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin").strip().lower()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
ALLOW_REGISTRATION = os.getenv("ALLOW_REGISTRATION", "1") == "1"
MAX_SQL_BYTES = 2 * 1024 * 1024


def load_secret_key():
    """SECRET_KEY do ambiente; localmente, gera uma e guarda em data/secret_key."""
    key = os.getenv("SECRET_KEY")
    if key:
        return key
    path = os.path.join(DATA_DIR, "secret_key")
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        os.makedirs(DATA_DIR, exist_ok=True)
        key = secrets.token_hex(32)
        with open(path, "w", encoding="utf-8") as f:
            f.write(key)
        return key


app = Flask(__name__, static_folder="static", static_url_path="/static")
app.config.update(
    SECRET_KEY=load_secret_key(),
    MAX_CONTENT_LENGTH=MAX_SQL_BYTES + 64 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("SESSION_COOKIE_SECURE", "0") == "1",
)


# ---------------------------------------------------------------------------
# Dados de demonstração: lidos de static/demo-data.js (fonte única,
# compartilhada com o modo Live Server).
# ---------------------------------------------------------------------------
def _js_template(s):
    return s.replace("\\`", "`").replace("\\\\", "\\")


def load_demo_data():
    with open(os.path.join(BASE_DIR, "static", "demo-data.js"), encoding="utf-8") as f:
        js = f.read()
    tables = {}
    for m in re.finditer(r"(\w+):\s*\{\s*ddl:\s*`((?:[^`\\]|\\.)*)`,\s*data:\s*`((?:[^`\\]|\\.)*)`", js):
        tables[m.group(1)] = {"ddl": _js_template(m.group(2)), "data": _js_template(m.group(3))}
    name = re.search(r"DEMO_DB_NAME\s*=\s*'([^']+)'", js).group(1)
    arr = re.search(r"TYPE_MAPPINGS\s*=\s*(\[.*\]);", js, re.S).group(1)
    mappings = ast.literal_eval(arr)
    return name, tables, mappings


DEMO_DB_NAME, DEMO_TABLES, MAPPINGS = load_demo_data()


# ---------------------------------------------------------------------------
# Banco de dados (SQLite ou PostgreSQL) com a mesma interface
# ---------------------------------------------------------------------------
def db_connect():
    if USE_PG:
        if psycopg is None:
            raise RuntimeError("Instale a dependência psycopg para usar PostgreSQL.")
        return psycopg.connect(DATABASE_URL, row_factory=dict_row)
    os.makedirs(os.path.dirname(SQLITE_PATH), exist_ok=True)
    conn = sqlite3.connect(SQLITE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def db(sql, params=(), fetch=None):
    """Executa SQL escrito com '?' (convertido para %s no PostgreSQL).

    fetch: None, "one" ou "all". Linhas são devolvidas como dict.
    """
    if USE_PG:
        sql = sql.replace("?", "%s")
    conn = db_connect()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        if fetch == "one":
            row = cur.fetchone()
            result = dict(row) if row is not None else None
        elif fetch == "all":
            result = [dict(r) for r in cur.fetchall()]
        else:
            result = None
        conn.commit()
        return result
    finally:
        conn.close()


def init_db():
    pk = "BIGSERIAL PRIMARY KEY" if USE_PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
    db(f"""CREATE TABLE IF NOT EXISTS users (
        id {pk},
        username VARCHAR(30) NOT NULL UNIQUE,
        name VARCHAR(120) NOT NULL,
        password_hash VARCHAR(255) NOT NULL,
        role VARCHAR(10) NOT NULL DEFAULT 'user',
        created_at VARCHAR(40) NOT NULL
    )""")
    db(f"""CREATE TABLE IF NOT EXISTS conversoes (
        id {pk},
        user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        db_name VARCHAR(255) NOT NULL,
        source_type VARCHAR(20) NOT NULL,
        mode VARCHAR(10) NOT NULL,
        tables_json TEXT NOT NULL,
        stats_json TEXT NOT NULL,
        log_json TEXT NOT NULL,
        source_sql TEXT NOT NULL,
        converted_sql TEXT NOT NULL,
        duration_ms INTEGER NOT NULL,
        created_at VARCHAR(40) NOT NULL
    )""")
    db("CREATE INDEX IF NOT EXISTS idx_conversoes_user ON conversoes (user_id, id)")

    # Administrador: senha do ambiente; localmente (SQLite, sem senha definida)
    # usa a conta de demonstração admin/demo123, igual ao modo Live Server.
    password = ADMIN_PASSWORD or ("" if USE_PG else "demo123")
    if not password:
        app.logger.warning("ADMIN_PASSWORD não definido: nenhum administrador criado.")
        return
    if not db("SELECT id FROM users WHERE username = ?", (ADMIN_USERNAME,), fetch="one"):
        db("INSERT INTO users (username, name, password_hash, role, created_at) VALUES (?, ?, ?, ?, ?)",
           (ADMIN_USERNAME, "Administrador", generate_password_hash(password), "admin", now_iso()))


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Autenticação
# ---------------------------------------------------------------------------
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,30}$")
_failures = defaultdict(deque)   # ip -> horários de tentativas falhas
MAX_FAILURES, WINDOW = 10, 300   # 10 erros em 5 minutos bloqueiam o IP


def too_many_failures(ip):
    q = _failures[ip]
    while q and q[0] < time.time() - WINDOW:
        q.popleft()
    return len(q) >= MAX_FAILURES


def current_user():
    uid = session.get("uid")
    if not uid:
        return None
    return db("SELECT id, username, name, role FROM users WHERE id = ?", (uid,), fetch="one")


def require_login(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user:
            session.clear()
            return jsonify({"message": "Sessão expirada. Entre novamente."}), 401
        return fn(user, *args, **kwargs)
    return wrapper


def me_payload(user):
    return {"authenticated": True, "username": user["username"], "name": user["name"], "role": user["role"]}


def start_session(user):
    session.clear()
    session["uid"] = user["id"]
    session.permanent = True


def validate_registration(name, username, password):
    if len(name) < 2:
        return "Informe seu nome."
    if not USERNAME_RE.match(username):
        return "O usuário deve ter de 3 a 30 caracteres: letras, números, ponto, hífen ou _."
    if len(password) < 6:
        return "A senha deve ter pelo menos 6 caracteres."
    if not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        return "A senha deve conter letras e números."
    return None


# ---------------------------------------------------------------------------
# Rotas
# ---------------------------------------------------------------------------
@app.get("/")
@app.get("/index.html")
def index():
    with open(os.path.join(BASE_DIR, "index.html"), encoding="utf-8") as f:
        html = f.read()
    html = html.replace("<!-- DBC_BACKEND:", "<script>window.DBC_BACKEND = 'flask';</script>\n<!-- DBC_BACKEND:", 1)
    return Response(html, mimetype="text/html")


@app.get("/health")
@app.get("/api/health")
def health():
    return jsonify({"status": "ok", "service": "DBConverter", "database": "postgresql" if USE_PG else "sqlite"})


@app.get("/api/me")
def me():
    user = current_user()
    return jsonify(me_payload(user) if user else {"authenticated": False})


@app.post("/api/login")
def login():
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "?").split(",")[0].strip()
    if too_many_failures(ip):
        return jsonify({"message": "Muitas tentativas. Aguarde alguns minutos e tente novamente."}), 429
    data = request.get_json(silent=True) or {}
    username = str(data.get("username", "")).strip().lower()
    password = str(data.get("password", ""))
    user = db("SELECT id, username, name, role, password_hash FROM users WHERE username = ?", (username,), fetch="one")
    if not user or not check_password_hash(user["password_hash"], password):
        _failures[ip].append(time.time())
        return jsonify({"message": "Usuário ou senha incorretos."}), 401
    _failures.pop(ip, None)
    start_session(user)
    return jsonify(me_payload(user))


@app.post("/api/register")
def register():
    if not ALLOW_REGISTRATION:
        return jsonify({"message": "O cadastro de novos usuários está desativado."}), 403
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()[:120]
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))
    err = validate_registration(name, username, password)
    if err:
        return jsonify({"message": err}), 400
    username = username.lower()
    if db("SELECT id FROM users WHERE username = ?", (username,), fetch="one"):
        return jsonify({"message": "Este nome de usuário já está em uso."}), 409
    try:
        db("INSERT INTO users (username, name, password_hash, role, created_at) VALUES (?, ?, ?, ?, ?)",
           (username, name, generate_password_hash(password), "user", now_iso()))
    except Exception:  # corrida: outro cadastro com o mesmo nome
        return jsonify({"message": "Este nome de usuário já está em uso."}), 409
    user = db("SELECT id, username, name, role FROM users WHERE username = ?", (username,), fetch="one")
    start_session(user)
    return jsonify(me_payload(user)), 201


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify({"success": True})


@app.post("/api/test-connection")
@require_login
def test_connection(user):
    data = request.get_json(silent=True) or {}
    dbname = str(data.get("dbname") or DEMO_DB_NAME).strip()[:255]
    tables = []
    for name, t in DEMO_TABLES.items():
        tables.append(converter.convert(t["ddl"] + "\n" + t["data"])["table_info"][0])
    return jsonify({"dbname": dbname, "tables": tables, "mode": "demo"})


@app.get("/api/mappings")
@require_login
def get_mappings(user):
    return jsonify(MAPPINGS)


def record_from_row(row, user, full=True):
    rec = {
        "id": row["id"], "username": user["username"], "user_name": user["name"],
        "db": row["db_name"], "source_type": row["source_type"], "mode": row["mode"],
        "stats": json.loads(row["stats_json"]), "created_at": row["created_at"],
        "duration_ms": row["duration_ms"],
    }
    if full:
        rec.update({
            "tables": json.loads(row["tables_json"]), "log": json.loads(row["log_json"]),
            "orig_sql": row["source_sql"], "sql": row["converted_sql"],
            "conv_sql": row["converted_sql"].split("\n\n", 1)[-1],
        })
    return rec


@app.post("/api/convert")
@require_login
def convert(user):
    data = request.get_json(silent=True) or {}
    from_sql = bool(data.get("from_sql"))
    include_data = bool(data.get("include_data", True))
    dbname = str(data.get("dbname") or DEMO_DB_NAME).strip()[:255]

    if from_sql:
        source = str(data.get("manual_sql") or "").strip()
        if not source:
            return jsonify({"message": "Informe o SQL."}), 400
        if len(source.encode("utf-8")) > MAX_SQL_BYTES:
            return jsonify({"message": "O SQL excede o limite de 2 MB."}), 400
        tables = None
    else:
        selected = data.get("tables") or []
        tables = [t for t in DEMO_TABLES if t in selected]
        if not tables:
            return jsonify({"message": "Selecione ao menos uma tabela."}), 400
        parts = [DEMO_TABLES[t]["ddl"] for t in tables]
        if include_data:
            parts += [DEMO_TABLES[t]["data"] for t in tables]
        source = "\n\n".join(parts)

    t0 = time.perf_counter()
    try:
        result = converter.convert(source, include_data=include_data)
    except ValueError as exc:
        return jsonify({"message": str(exc)}), 400
    duration = round((time.perf_counter() - t0) * 1000)

    label = "SQL manual" if from_sql else dbname
    header = ("-- DBConverter | MySQL/MariaDB -> PostgreSQL\n"
              f"-- Banco: {label}\n"
              f"-- Gerado em: {datetime.now().strftime('%d/%m/%Y, %H:%M:%S')} por {user['username']}\n\n")
    created = now_iso()
    row = {
        "user_id": user["id"], "db_name": label, "source_type": "sql_manual" if from_sql else "demo",
        "mode": "ddl+dml" if include_data else "ddl",
        "tables_json": json.dumps(tables or result["tables"], ensure_ascii=False),
        "stats_json": json.dumps(result["stats"], ensure_ascii=False),
        "log_json": json.dumps(result["log"], ensure_ascii=False),
        "source_sql": source, "converted_sql": header + result["sql"],
        "duration_ms": duration, "created_at": created,
    }
    cols = list(row)
    sql = f"INSERT INTO conversoes ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})"
    if USE_PG:
        new = db(sql + " RETURNING id", tuple(row.values()), fetch="one")
    else:
        conn = db_connect()
        try:
            cur = conn.execute(sql, tuple(row.values()))
            conn.commit()
            new = {"id": cur.lastrowid}
        finally:
            conn.close()
    row["id"] = new["id"]
    return jsonify(record_from_row(row, user))


@app.get("/api/history")
@require_login
def history(user):
    rows = db("SELECT id, db_name, source_type, mode, stats_json, created_at, duration_ms "
              "FROM conversoes WHERE user_id = ? ORDER BY id DESC LIMIT 200", (user["id"],), fetch="all")
    return jsonify([record_from_row(r, user, full=False) for r in rows])


@app.get("/api/history/<int:conversion_id>")
@require_login
def history_detail(user, conversion_id):
    row = db("SELECT * FROM conversoes WHERE id = ? AND user_id = ?", (conversion_id, user["id"]), fetch="one")
    if not row:
        return jsonify({"message": "Conversão não encontrada."}), 404
    return jsonify(record_from_row(row, user))


@app.delete("/api/history/<int:conversion_id>")
@require_login
def history_delete(user, conversion_id):
    row = db("SELECT id FROM conversoes WHERE id = ? AND user_id = ?", (conversion_id, user["id"]), fetch="one")
    if not row:
        return jsonify({"message": "Conversão não encontrada."}), 404
    db("DELETE FROM conversoes WHERE id = ? AND user_id = ?", (conversion_id, user["id"]))
    return jsonify({"success": True})


@app.errorhandler(413)
def too_large(_):
    return jsonify({"message": "Arquivo ou requisição muito grande (limite de 2 MB)."}), 413


@app.errorhandler(500)
def server_error(_):
    return jsonify({"message": "Erro interno no servidor."}), 500


init_db()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    print(f"\n  DBConverter rodando em http://127.0.0.1:{port}\n")
    app.run(host="127.0.0.1", port=port, debug=os.getenv("FLASK_DEBUG") == "1")
