"""
Conversor de scripts MySQL/MariaDB -> PostgreSQL.

O conversor trabalha em três fases (ETL):
  1. Extract   - divide o script em instruções e cada CREATE TABLE em
                 definições (colunas, chaves, índices, opções de tabela);
  2. Transform - mapeia tipos, valores padrão e restrições para PostgreSQL;
  3. Load      - monta um script PostgreSQL ordenado em seções:
                 tabelas -> dados -> índices -> sequências -> chaves estrangeiras.

As chaves estrangeiras são criadas ao final (ALTER TABLE ... ADD CONSTRAINT),
como faz o pg_dump. Assim as tabelas e os dados podem ser carregados em
qualquer ordem, sem depender da ordem do script original.

O mesmo algoritmo existe em JavaScript (static/converter.js), usado quando o
projeto roda pelo Live Server. Ao alterar um, altere o outro.
"""
import re

# ---------------------------------------------------------------------------
# Utilidades léxicas
# ---------------------------------------------------------------------------

_MYSQL_ESCAPES = {
    "0": "", "'": "'", '"': '"', "b": "\b", "n": "\n", "r": "\r",
    "t": "\t", "Z": "\x1a", "\\": "\\", "%": "\\%", "_": "\\_",
}


def ident(name):
    """Normaliza um nome: o PostgreSQL trata nomes sem aspas em minúsculas.

    Converter tudo para minúsculas (como faz o pgloader) permite que as
    consultas da aplicação continuem funcionando sem aspas.
    """
    return str(name).lower()


def q(name):
    """Identificador PostgreSQL entre aspas duplas."""
    return '"' + ident(name).replace('"', '""') + '"'


def pg_str(value):
    """Literal de texto padrão do PostgreSQL."""
    return "'" + str(value).replace("'", "''") + "'"


def read_quoted(s, i):
    """Lê um literal entre aspas a partir de s[i]; devolve (texto, índice_final)."""
    quote = s[i]
    out = []
    j = i + 1
    n = len(s)
    while j < n:
        c = s[j]
        if c == "\\" and quote != "`" and j + 1 < n:
            out.append(_MYSQL_ESCAPES.get(s[j + 1], s[j + 1]))
            j += 2
            continue
        if c == quote:
            if j + 1 < n and s[j + 1] == quote:
                out.append(quote)
                j += 2
                continue
            return "".join(out), j + 1
        out.append(c)
        j += 1
    return "".join(out), n


def match_paren(s, i):
    """Índice do ')' que fecha o '(' em s[i], ignorando texto entre aspas."""
    depth = 0
    j = i
    n = len(s)
    while j < n:
        c = s[j]
        if c in "'\"`":
            _, j = read_quoted(s, j)
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return j
        j += 1
    return n - 1


_WORD_STOP = set(" \t\r\n'\"`(),;=")


def tokenize(s):
    """Tokens: (tipo, valor). Tipos: str, dq, bq, group, word, punct."""
    toks = []
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c.isspace():
            i += 1
        elif c == "'":
            v, i = read_quoted(s, i)
            toks.append(("str", v))
        elif c == '"':
            v, i = read_quoted(s, i)
            toks.append(("dq", v))
        elif c == "`":
            v, i = read_quoted(s, i)
            toks.append(("bq", v))
        elif c == "(":
            j = match_paren(s, i)
            toks.append(("group", s[i + 1:j]))
            i = j + 1
        elif c in "),;=":
            toks.append(("punct", c))
            i += 1
        else:
            j = i
            while j < n and s[j] not in _WORD_STOP:
                j += 1
            toks.append(("word", s[i:j]))
            i = j
    return toks


def split_top(s, sep=","):
    """Divide s por `sep` apenas no nível 0 de parênteses e fora de aspas."""
    parts, buf, depth, i, n = [], [], 0, 0, len(s)
    while i < n:
        c = s[i]
        if c in "'\"`":
            _, j = read_quoted(s, i)
            buf.append(s[i:j])
            i = j
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        if c == sep and depth == 0:
            parts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(c)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return [p for p in parts if p]


def fragment(s, dq="string"):
    """Converte um trecho livre: `ident` -> "ident", strings MySQL -> padrão SQL.

    dq="string": "texto" é literal (comportamento padrão do MySQL);
    dq="ident" : "nome" é identificador (listas de colunas, nomes).
    """
    out, i, n = [], 0, len(s)
    plain = []

    def flush():
        if plain:
            txt = "".join(plain)
            txt = re.sub(r"\bNOW\s*\(\s*\)", "CURRENT_TIMESTAMP", txt, flags=re.I)
            txt = re.sub(r"\bIFNULL\s*\(", "COALESCE(", txt, flags=re.I)
            out.append(txt)
            plain.clear()

    while i < n:
        c = s[i]
        if c == "`":
            flush()
            v, i = read_quoted(s, i)
            out.append(q(v))
        elif c == "'":
            flush()
            v, i = read_quoted(s, i)
            out.append(pg_str(v))
        elif c == '"':
            flush()
            v, i = read_quoted(s, i)
            out.append(q(v) if dq == "ident" else pg_str(v))
        else:
            plain.append(c)
            i += 1
    flush()
    return "".join(out)


_DELIM_RE = re.compile(r"[ \t]*DELIMITER[ \t]+(\S+)[ \t]*(?:\r?\n|$)", re.I)


def split_statements(sql):
    """Divide o script em instruções, removendo comentários.

    Devolve lista de (texto, tipo), onde tipo é "sql" ou "routine"
    (blocos delimitados por DELIMITER, como procedures e triggers).
    """
    stmts, buf = [], []
    i, n = 0, len(sql)
    delim = ";"
    at_line_start = True
    while i < n:
        c = sql[i]
        if at_line_start:
            m = _DELIM_RE.match(sql, i)
            if m:
                if "".join(buf).strip():
                    stmts.append(("".join(buf).strip(), "sql" if delim == ";" else "routine"))
                buf = []
                delim = m.group(1)
                i = m.end()
                continue
        at_line_start = False
        if c in "'\"`":
            _, j = read_quoted(sql, i)
            buf.append(sql[i:j])
            i = j
            continue
        if c == "#" or (sql.startswith("--", i) and (i + 2 >= n or sql[i + 2] in " \t\r\n")):
            j = sql.find("\n", i)
            i = n if j < 0 else j
            continue
        if sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if sql.startswith(delim, i):
            if "".join(buf).strip():
                stmts.append(("".join(buf).strip(), "sql" if delim == ";" else "routine"))
            buf = []
            i += len(delim)
            continue
        if c == "\n":
            at_line_start = True
        buf.append(c)
        i += 1
    if "".join(buf).strip():
        stmts.append(("".join(buf).strip(), "sql" if delim == ";" else "routine"))
    return stmts


def is_word(tok, *words):
    return tok is not None and tok[0] == "word" and tok[1].upper() in words


def ident_of(tok):
    """Nome de identificador a partir de um token (bq, dq ou word)."""
    if tok is None:
        return None
    if tok[0] in ("bq", "dq", "word"):
        return tok[1]
    return None


# ---------------------------------------------------------------------------
# Mapeamento de tipos
# ---------------------------------------------------------------------------

INT_TYPES = {"TINYINT", "SMALLINT", "MEDIUMINT", "INT", "INTEGER", "BIGINT"}
INT_SIGNED = {"TINYINT": "SMALLINT", "SMALLINT": "SMALLINT", "MEDIUMINT": "INTEGER",
              "INT": "INTEGER", "INTEGER": "INTEGER", "BIGINT": "BIGINT"}
INT_UNSIGNED = {"TINYINT": "SMALLINT", "SMALLINT": "INTEGER", "MEDIUMINT": "INTEGER",
                "INT": "BIGINT", "INTEGER": "BIGINT", "BIGINT": "BIGINT"}
INT_SERIAL = {"TINYINT": "SMALLSERIAL", "SMALLINT": "SMALLSERIAL", "MEDIUMINT": "SERIAL",
              "INT": "SERIAL", "INTEGER": "SERIAL", "BIGINT": "BIGSERIAL"}
TEXT_TYPES = {"TINYTEXT", "TEXT", "MEDIUMTEXT", "LONGTEXT"}
BLOB_TYPES = {"BINARY", "VARBINARY", "TINYBLOB", "BLOB", "MEDIUMBLOB", "LONGBLOB"}
SPATIAL_TYPES = {"GEOMETRY", "POINT", "LINESTRING", "POLYGON", "MULTIPOINT",
                 "MULTILINESTRING", "MULTIPOLYGON", "GEOMETRYCOLLECTION"}


def map_type(base, args, unsigned, zerofill, auto_inc):
    """Devolve dict(pg, st, notes, nonneg, enum_vals)."""
    b = base.upper()
    a = (args or "").strip()
    notes, st, nonneg = [], "ok", False
    src = b + (f"({a})" if a else "") + (" UNSIGNED" if unsigned else "")

    if b in INT_TYPES:
        if b == "TINYINT" and a == "1" and not auto_inc:
            return dict(src=src, pg="BOOLEAN", st="ok", notes=["TINYINT(1) é o booleano do MySQL"], nonneg=False)
        if a:
            notes.append(f"largura de exibição ({a}) removida")
        if auto_inc:
            pg = INT_SERIAL[b]
            notes.insert(0, "AUTO_INCREMENT vira sequência automática")
            if unsigned:
                notes.append("UNSIGNED dispensado: a sequência só gera positivos")
        elif unsigned:
            pg = INT_UNSIGNED[b]
            nonneg, st = True, "warn"
            if b == "BIGINT":
                notes.append("UNSIGNED -> CHECK >= 0 (valores acima de 9,2e18 não cabem)")
            elif pg != INT_SIGNED[b]:
                notes.append(f"UNSIGNED -> {pg} para caber a faixa + CHECK >= 0")
            else:
                notes.append("UNSIGNED -> CHECK >= 0")
        else:
            pg = INT_SIGNED[b]
            if pg != b:
                notes.append("tipo equivalente mais próximo")
        if zerofill:
            st = "warn"
            notes.append("ZEROFILL não existe: formate na consulta (LPAD)")
        return dict(src=src, pg=pg, st=st, notes=notes, nonneg=nonneg)

    if b in ("DECIMAL", "NUMERIC", "DEC", "FIXED"):
        pg = "DECIMAL" + (f"({a})" if a else "")
        if unsigned:
            nonneg, st = True, "warn"
            notes.append("UNSIGNED -> CHECK >= 0")
        return dict(src=src, pg=pg, st=st, notes=notes, nonneg=nonneg)

    if b == "FLOAT":
        pg = "REAL"
        if a:
            parts = [p.strip() for p in a.split(",")]
            if len(parts) == 1 and parts[0].isdigit() and int(parts[0]) > 24:
                pg = "DOUBLE PRECISION"
            elif len(parts) == 2:
                st = "warn"
                notes.append("FLOAT(M,D) sem equivalente; considere DECIMAL")
        if unsigned:
            nonneg, st = True, "warn"
            notes.append("UNSIGNED -> CHECK >= 0")
        return dict(src=src, pg=pg, st=st, notes=notes, nonneg=nonneg)

    if b in ("DOUBLE", "DOUBLE PRECISION", "REAL"):
        if a:
            st = "warn"
            notes.append("precisão (M,D) descartada; considere DECIMAL")
        if unsigned:
            nonneg, st = True, "warn"
            notes.append("UNSIGNED -> CHECK >= 0")
        return dict(src=src, pg="DOUBLE PRECISION", st=st, notes=notes, nonneg=nonneg)

    if b == "BIT":
        if a in ("", "1"):
            return dict(src=src, pg="BOOLEAN", st="ok", notes=["BIT(1) usado como booleano"], nonneg=False)
        return dict(src=src, pg=f"BIT({a})", st="ok", notes=notes, nonneg=False)

    if b in ("BOOL", "BOOLEAN"):
        return dict(src=src, pg="BOOLEAN", st="ok", notes=notes, nonneg=False)

    if b in ("CHAR", "VARCHAR", "NCHAR", "NVARCHAR", "CHARACTER", "CHARACTER VARYING"):
        pg = {"NCHAR": "CHAR", "NVARCHAR": "VARCHAR", "CHARACTER": "CHAR",
              "CHARACTER VARYING": "VARCHAR"}.get(b, b)
        return dict(src=src, pg=pg + (f"({a})" if a else ""), st="ok", notes=notes, nonneg=False)

    if b in TEXT_TYPES:
        if b != "TEXT":
            notes.append("unificado em TEXT")
        return dict(src=src, pg="TEXT", st="ok", notes=notes, nonneg=False)

    if b in BLOB_TYPES:
        notes.append("binário -> BYTEA")
        return dict(src=src, pg="BYTEA", st="ok", notes=notes, nonneg=False)

    if b == "DATE":
        return dict(src=src, pg="DATE", st="ok", notes=notes, nonneg=False)
    if b in ("DATETIME", "TIMESTAMP"):
        if b == "DATETIME":
            notes.append("DATETIME -> TIMESTAMP")
        return dict(src=src, pg="TIMESTAMP" + (f"({a})" if a else ""), st="ok", notes=notes, nonneg=False)
    if b == "TIME":
        return dict(src=src, pg="TIME" + (f"({a})" if a else ""), st="ok", notes=notes, nonneg=False)
    if b == "YEAR":
        return dict(src=src, pg="SMALLINT", st="warn", notes=["YEAR -> SMALLINT (sem validação de faixa)"], nonneg=False)
    if b == "JSON":
        return dict(src=src, pg="JSONB", st="ok", notes=["JSON -> JSONB (binário, indexável)"], nonneg=False)

    if b == "ENUM":
        vals = [v for (k, v) in tokenize(a) if k in ("str", "dq")]
        size = max([len(v) for v in vals] + [1])
        return dict(src="ENUM(" + ",".join(pg_str(v) for v in vals) + ")",
                    pg=f"VARCHAR({size})", st="warn",
                    notes=["ENUM -> VARCHAR + CHECK; revise os valores permitidos"],
                    nonneg=False, enum_vals=vals)
    if b == "SET":
        vals = [v for (k, v) in tokenize(a) if k in ("str", "dq")]
        size = max(sum(len(v) for v in vals) + len(vals), 1)
        return dict(src=src, pg=f"VARCHAR({size})", st="warn",
                    notes=["SET -> VARCHAR com valores separados por vírgula (sem validação)"],
                    nonneg=False)

    if b in SPATIAL_TYPES:
        return dict(src=src, pg=b, st="err",
                    notes=["tipo espacial: use a extensão PostGIS para equivalência completa"], nonneg=False)

    return dict(src=src, pg=b + (f"({a})" if a else ""), st="warn",
                notes=["tipo sem mapeamento conhecido; mantido como está"], nonneg=False)


# ---------------------------------------------------------------------------
# Conversor
# ---------------------------------------------------------------------------

ZERO_DATE = re.compile(r"^0000-00-00")


class Converter:
    def __init__(self, include_data=True):
        self.include_data = include_data
        self.log = []
        self.drops, self.tables, self.body, self.indexes = [], [], [], []
        self.fks, self.comments = [], []
        self.schema = {}          # tabela -> [(coluna, tipo_pg)]
        self.serial_cols = {}     # tabela -> coluna sequencial
        self.auto_start = {}      # tabela -> valor de AUTO_INCREMENT=n
        self.data_tables = set()  # tabelas que receberam INSERT
        self.relnames = set()     # nomes de relações/índices já usados
        self.info = {}            # tabela -> {"fks": n, "rows": n}
        self.stats = dict(tables=0, cols=0, indexes=0, fks=0, rows=0,
                          skipped_inserts=0, warn=0, err=0)

    # -- log --------------------------------------------------------------
    def add(self, tbl, col, src, dst, st, note):
        self.log.append({"tbl": tbl or "—", "col": col or "—", "from": src,
                         "to": dst, "st": st, "note": note})

    # -- nomes ------------------------------------------------------------
    def unique_name(self, name, table):
        name, table = ident(name), ident(table)
        base = name[:63]
        if base not in self.relnames:
            self.relnames.add(base)
            return base, False
        cand = f"{table}_{name}"[:63]
        k = 2
        while cand in self.relnames:
            cand = f"{table}_{name}"[:60] + f"_{k}"
            k += 1
        self.relnames.add(cand)
        return cand, True

    # -- listas de colunas de índices ----------------------------------------
    def index_cols(self, inner, table):
        cols, out, prefix = [], [], False
        for part in split_top(inner):
            toks = tokenize(part)
            if not toks:
                continue
            if toks[0][0] == "group":
                out.append("(" + fragment(toks[0][1]) + ")")
                continue
            name = ident_of(toks[0])
            cols.append(name)
            piece = q(name)
            rest = toks[1:]
            if rest and rest[0][0] == "group":
                prefix = True
                rest = rest[1:]
            for t in rest:
                if is_word(t, "ASC", "DESC"):
                    piece += " " + t[1].upper()
            out.append(piece)
        if prefix:
            self.add(table, ", ".join(cols), "índice com prefixo col(n)", "índice na coluna inteira",
                     "warn", "PostgreSQL não tem índice por prefixo; o índice cobre o valor completo")
        return "(" + ", ".join(out) + ")", cols

    # -- valores padrão ------------------------------------------------------
    def convert_default(self, toks, i, pg_type, table, col):
        """Lê o valor após DEFAULT; devolve (texto_ou_None, novo_i)."""
        t = toks[i] if i < len(toks) else None
        if t is None:
            return None, i
        upper_type = pg_type.upper()
        nxt = toks[i + 1] if i + 1 < len(toks) else None

        # b'0' / x'AF'
        if t[0] == "word" and t[1].lower() in ("b", "x") and nxt and nxt[0] == "str":
            raw = nxt[1]
            i += 2
            if upper_type == "BOOLEAN":
                return ("TRUE" if raw.strip("0") else "FALSE"), i
            if t[1].lower() == "b":
                return f"B'{raw}'", i
            return f"'\\x{raw}'", i
        # introdutor de charset: _utf8mb4'abc'
        if t[0] == "word" and t[1].startswith("_") and nxt and nxt[0] == "str":
            t, i = nxt, i + 1

        if t[0] in ("str", "dq"):
            val = t[1]
            i += 1
            if upper_type == "BOOLEAN" and val in ("0", "1"):
                return ("TRUE" if val == "1" else "FALSE"), i
            if upper_type.startswith(("TIMESTAMP", "DATE")) and ZERO_DATE.match(val):
                self._zero_default = True
                return "", i
            return pg_str(val), i
        if t[0] == "group":
            self.add(table, col, "DEFAULT (expressão)", "DEFAULT (expressão)", "warn",
                     "expressão padrão copiada; confira a sintaxe")
            return "(" + fragment(t[1]) + ")", i + 1
        if t[0] == "word":
            w = t[1].upper()
            i += 1
            if w in ("CURRENT_TIMESTAMP", "NOW", "LOCALTIME", "LOCALTIMESTAMP"):
                if i < len(toks) and toks[i][0] == "group":
                    i += 1
                return "CURRENT_TIMESTAMP", i
            if upper_type == "BOOLEAN" and w in ("0", "1", "TRUE", "FALSE"):
                return ("TRUE" if w in ("1", "TRUE") else "FALSE"), i
            return t[1], i
        return None, i + 1

    # -- colunas ----------------------------------------------------------
    def column(self, toks, table, alter=False):
        """Converte a definição de uma coluna. Devolve dict com sql e metadados."""
        if not toks or ident_of(toks[0]) is None:
            raise ValueError("definição de coluna inválida")
        name = ident_of(toks[0])
        i = 1
        base = toks[i][1].upper() if i < len(toks) and toks[i][0] == "word" else "TEXT"
        i += 1
        if base == "DOUBLE" and is_word(toks[i] if i < len(toks) else None, "PRECISION"):
            base, i = "DOUBLE PRECISION", i + 1
        if base == "CHARACTER" and is_word(toks[i] if i < len(toks) else None, "VARYING"):
            base, i = "CHARACTER VARYING", i + 1
        args = None
        if i < len(toks) and toks[i][0] == "group":
            args = toks[i][1]
            i += 1
        unsigned = zerofill = False
        while i < len(toks) and is_word(toks[i], "UNSIGNED", "SIGNED", "ZEROFILL"):
            w = toks[i][1].upper()
            unsigned = unsigned or w in ("UNSIGNED", "ZEROFILL")
            zerofill = zerofill or w == "ZEROFILL"
            i += 1
        rest = toks[i:]
        auto_inc = any(is_word(t, "AUTO_INCREMENT") for t in rest)

        self._zero_default = False
        m = map_type(base, args, unsigned, zerofill, auto_inc)
        pg_type = m["pg"]
        notes = list(m["notes"])
        st = m["st"]
        parts = [q(name), pg_type]
        comment = None
        not_null = None
        default = None
        inline_pk = False

        j = 0
        while j < len(rest):
            t = rest[j]
            w = t[1].upper() if t[0] == "word" else None
            nxt = rest[j + 1] if j + 1 < len(rest) else None
            if w == "NOT" and is_word(nxt, "NULL"):
                parts.append("NOT NULL")
                not_null = True
                j += 2
            elif w == "NULL":
                parts.append("NULL")
                not_null = False
                j += 1
            elif w == "DEFAULT":
                val, j = self.convert_default(rest, j + 1, pg_type, table, name)
                if auto_inc:
                    continue
                if val:
                    parts.append("DEFAULT " + val)
                    default = val
            elif w == "AUTO_INCREMENT":
                j += 1
            elif w in ("PRIMARY", "KEY"):
                parts.append("PRIMARY KEY")
                inline_pk = True
                j += 2 if (w == "PRIMARY" and is_word(nxt, "KEY")) else 1
            elif w == "UNIQUE":
                parts.append("UNIQUE")
                j += 2 if is_word(nxt, "KEY") else 1
            elif w == "COMMENT":
                if nxt and nxt[0] in ("str", "dq"):
                    comment = nxt[1]
                    j += 2
                else:
                    j += 1
            elif w == "ON" and is_word(nxt, "UPDATE"):
                j += 3
                if j < len(rest) and rest[j][0] == "group":
                    j += 1
                st = "warn" if st == "ok" else st
                notes.append("ON UPDATE CURRENT_TIMESTAMP não existe: use um trigger")
            elif w in ("CHARACTER", "CHARSET"):
                j += 1
                if is_word(rest[j] if j < len(rest) else None, "SET"):
                    j += 1
                if j < len(rest) and rest[j][0] == "punct":
                    j += 1
                j += 1
                notes.append("charset da coluna removido")
            elif w == "COLLATE":
                j += 1
                if j < len(rest) and rest[j][0] == "punct":
                    j += 1
                j += 1
                notes.append("collation removida")
            elif w == "BINARY" and pg_type.startswith(("VARCHAR", "CHAR", "TEXT")):
                j += 1
            elif w in ("VISIBLE", "INVISIBLE", "SIGNED"):
                j += 1
            elif w == "CHECK" and nxt and nxt[0] == "group":
                parts.append("CHECK (" + fragment(nxt[1]) + ")")
                j += 2
            elif w in ("GENERATED", "AS"):
                k = j
                while k < len(rest) and rest[k][0] != "group":
                    k += 1
                expr = rest[k][1] if k < len(rest) else ""
                k += 1
                if k < len(rest) and is_word(rest[k], "VIRTUAL", "STORED"):
                    k += 1
                parts.append(f"GENERATED ALWAYS AS ({fragment(expr)}) STORED")
                st = "warn" if st == "ok" else st
                notes.append("coluna gerada: PostgreSQL só aceita STORED")
                j = k
            elif w == "REFERENCES":
                parts.append(" ".join(self.tok_text(x, "ident") for x in rest[j:]))
                j = len(rest)
            else:
                parts.append(self.tok_text(t))
                j += 1

        if self._zero_default:
            st = "warn" if st == "ok" else st
            notes.append("padrão '0000-00-00' removido (data inválida no PostgreSQL)")
            if not_null:
                parts.remove("NOT NULL")
                not_null = None
                notes.append("NOT NULL removido: datas zero são migradas como NULL")
        if m.get("enum_vals") is not None:
            parts.append(f"CHECK ({q(name)} IN (" + ", ".join(pg_str(v) for v in m["enum_vals"]) + "))")
        if m["nonneg"]:
            parts.append(f"CHECK ({q(name)} >= 0)")

        if not notes:
            notes.append("conversão direta" if m["src"].split("(")[0] == pg_type.split("(")[0]
                         else "tipo equivalente")
        return dict(name=name, sql=" ".join(parts), pg=pg_type, src=m["src"], st=st,
                    note="; ".join(notes), comment=comment, serial=auto_inc,
                    not_null=not_null, default=default, inline_pk=inline_pk)

    def tok_text(self, t, dq="string"):
        k, v = t
        if k == "bq":
            return q(v)
        if k == "dq":
            return q(v) if dq == "ident" else pg_str(v)
        if k == "str":
            return pg_str(v)
        if k == "group":
            return "(" + fragment(v, dq) + ")"
        return v

    # -- CREATE TABLE ------------------------------------------------------
    def create_table(self, stmt):
        m = re.match(r"CREATE\s+(TEMPORARY\s+)?TABLE\s*(IF\s+NOT\s+EXISTS\s+)?", stmt, re.I)
        pos = m.end()
        # nome (possivelmente qualificado: banco.tabela)
        open_idx = None
        j = pos
        while j < len(stmt):
            c = stmt[j]
            if c in "`\"'":
                _, j = read_quoted(stmt, j)
                continue
            if c == "(":
                open_idx = j
                break
            if re.match(r"\s+(LIKE|AS|SELECT)\b", stmt[j:], re.I):
                break
            j += 1
        name_toks = [t for t in tokenize(stmt[pos:j]) if t[0] in ("bq", "dq", "word")]
        name_parts = []
        for t in name_toks:
            name_parts.extend(p for p in (t[1].split(".") if t[0] == "word" else [t[1]]) if p)
        table = name_parts[-1] if name_parts else "tabela"
        prefix = "CREATE " + ("TEMPORARY " if m.group(1) else "") + "TABLE " + \
                 ("IF NOT EXISTS " if m.group(2) else "")

        if open_idx is None:
            like = re.search(r"\bLIKE\s+(.+)$", stmt[j:], re.I | re.S)
            if like:
                src = [t for t in tokenize(like.group(1)) if t[0] in ("bq", "dq", "word")]
                src_name = src[-1][1].split(".")[-1] if src else "?"
                self.tables.append(f"{prefix}{q(table)} (LIKE {q(src_name)} INCLUDING ALL);")
                self.add(table, None, "CREATE TABLE ... LIKE", "(LIKE ... INCLUDING ALL)", "ok",
                         "cópia de estrutura")
                return
            self.body.append(fragment(stmt) + ";")
            self.add(table, None, "CREATE TABLE ... AS SELECT", "copiado", "warn", "revise a consulta")
            return

        close_idx = match_paren(stmt, open_idx)
        body = stmt[open_idx + 1:close_idx]
        options = stmt[close_idx + 1:]
        self.stats["tables"] += 1
        self.relnames.add(ident(table))
        self.schema[ident(table)] = []

        lines, extra_comments = [], []
        for d in split_top(body):
            toks = tokenize(d)
            if not toks:
                continue
            first = toks[0][1].upper() if toks[0][0] == "word" else None
            second = toks[1][1].upper() if len(toks) > 1 and toks[1][0] == "word" else None

            cname = None
            if first == "CONSTRAINT":
                # CONSTRAINT [nome] PRIMARY KEY / UNIQUE / FOREIGN KEY / CHECK
                if len(toks) > 1 and not is_word(toks[1], "PRIMARY", "UNIQUE", "FOREIGN", "CHECK"):
                    cname = ident_of(toks[1])
                    toks = toks[2:]
                else:
                    toks = toks[1:]
                first = toks[0][1].upper() if toks and toks[0][0] == "word" else None
                second = toks[1][1].upper() if len(toks) > 1 and toks[1][0] == "word" else None

            if first == "PRIMARY":
                grp = next((t for t in toks if t[0] == "group"), None)
                cols_sql, _ = self.index_cols(grp[1] if grp else "", table)
                lines.append("PRIMARY KEY " + cols_sql)
                self.relnames.add(ident(f"{table}_pkey"))
            elif first == "UNIQUE":
                grp_i = next((k for k, t in enumerate(toks) if t[0] == "group"), None)
                name_tok = None
                for t in toks[1:grp_i]:
                    if not is_word(t, "KEY", "INDEX"):
                        name_tok = t
                        break
                iname = cname or ident_of(name_tok)
                cols_sql, cols = self.index_cols(toks[grp_i][1] if grp_i is not None else "", table)
                if iname:
                    final, renamed = self.unique_name(iname, table)
                    lines.append(f"CONSTRAINT {q(final)} UNIQUE {cols_sql}")
                    note = "UNIQUE KEY -> CONSTRAINT UNIQUE"
                    if renamed:
                        note += f"; renomeado (nomes de índice são únicos por schema no PostgreSQL)"
                    self.add(table, ", ".join(cols), f"UNIQUE KEY {iname}", f"CONSTRAINT {final} UNIQUE",
                             "ok", note)
                else:
                    lines.append(f"UNIQUE {cols_sql}")
            elif first in ("KEY", "INDEX", "FULLTEXT", "SPATIAL"):
                self.table_index(toks, table, first)
            elif first == "FOREIGN":
                self.foreign_key(toks, table, cname)
            elif first == "CHECK":
                grp = next((t for t in toks if t[0] == "group"), ("group", ""))
                c = (f"CONSTRAINT {q(cname)} " if cname else "") + "CHECK (" + fragment(grp[1]) + ")"
                lines.append(c)
            else:
                col = self.column(toks, table)
                lines.append(col["sql"])
                self.stats["cols"] += 1
                self.schema[ident(table)].append((ident(col["name"]), col["pg"]))
                if col["serial"]:
                    self.serial_cols[ident(table)] = col["name"]
                self.add(table, col["name"], col["src"], col["pg"], col["st"], col["note"])
                if col["comment"]:
                    extra_comments.append(
                        f"COMMENT ON COLUMN {q(table)}.{q(col['name'])} IS {pg_str(col['comment'])};")

        if not self.schema[ident(table)]:
            raise ValueError("CREATE TABLE sem colunas")
        table_comment = self.table_options(options, table)
        self.tables.append(prefix + q(table) + " (\n  " + ",\n  ".join(lines) + "\n);")
        if table_comment is not None:
            self.comments.append(f"COMMENT ON TABLE {q(table)} IS {pg_str(table_comment)};")
        self.comments.extend(extra_comments)

    def table_options(self, options, table):
        toks = tokenize(options)
        removed, comment, i = [], None, 0
        while i < len(toks):
            t = toks[i]
            if t[0] != "word":
                i += 1
                continue
            w = t[1].upper()
            if w == "PARTITION":
                self.add(table, None, "PARTITION BY", "removido", "warn",
                         "particionamento deve ser recriado manualmente (PARTITION OF)")
                break
            if w == "DEFAULT":
                i += 1
                continue
            name = w
            i += 1
            if w == "CHARACTER" and is_word(toks[i] if i < len(toks) else None, "SET"):
                name, i = "CHARACTER SET", i + 1
            if i < len(toks) and toks[i] == ("punct", "="):
                i += 1
            val = toks[i] if i < len(toks) else None
            i += 1
            if w == "COMMENT" and val and val[0] in ("str", "dq"):
                comment = val[1]
                continue
            if w == "AUTO_INCREMENT" and val and val[1].isdigit():
                self.auto_start[ident(table)] = int(val[1])
                continue
            removed.append(name)
        if removed:
            self.add(table, None, ", ".join(dict.fromkeys(removed)), "removido", "ok",
                     "opções de armazenamento exclusivas do MySQL")
        return comment

    def table_index(self, toks, table, kind):
        grp_i = next((k for k, t in enumerate(toks) if t[0] == "group"), None)
        if grp_i is None:
            return
        iname = None
        for t in toks[1:grp_i]:
            if not is_word(t, "KEY", "INDEX"):
                iname = ident_of(t)
                break
        cols_sql, cols = self.index_cols(toks[grp_i][1], table)
        if not iname:
            iname = f"{table}_{'_'.join(cols)}_idx"
        final, renamed = self.unique_name(iname, table)
        note = "índice criado com CREATE INDEX"
        if renamed:
            note += "; renomeado (nomes de índice são únicos por schema no PostgreSQL)"
        if kind == "SPATIAL":
            self.add(table, ", ".join(cols), "SPATIAL INDEX", "não convertido", "err",
                     "índice espacial requer PostGIS (USING GIST)")
            return
        if kind == "FULLTEXT":
            expr = " || ' ' || ".join(f"COALESCE({q(c)}, '')" for c in cols)
            self.indexes.append(f"CREATE INDEX {q(final)} ON {q(table)} USING GIN "
                                f"(to_tsvector('simple', {expr}));")
            self.add(table, ", ".join(cols), "FULLTEXT", "GIN + to_tsvector", "warn",
                     "busca textual: troque MATCH ... AGAINST por @@ to_tsquery")
        else:
            self.indexes.append(f"CREATE INDEX {q(final)} ON {q(table)} {cols_sql};")
            self.add(table, ", ".join(cols), f"KEY {iname}", f"CREATE INDEX {final}", "ok", note)
        self.stats["indexes"] += 1

    def foreign_key(self, toks, table, cname):
        # FOREIGN KEY [nome_indice] (cols) REFERENCES tabela (cols) [ON ...]
        i = 2 if is_word(toks[1] if len(toks) > 1 else None, "KEY") else 1
        while i < len(toks) and toks[i][0] != "group":
            i += 1  # MySQL permite um nome de índice aqui; o PostgreSQL não
        if i >= len(toks):
            return
        cols_sql, cols = self.index_cols(toks[i][1], table)
        rest = toks[i + 1:]
        ref_table = None
        out = []
        k = 0
        while k < len(rest):
            t = rest[k]
            if is_word(t, "REFERENCES"):
                out.append("REFERENCES")
                k += 1
                name_tok = rest[k] if k < len(rest) else None
                ref = ident_of(name_tok) or ""
                if name_tok and name_tok[0] == "word" and "." in ref:
                    ref = ref.split(".")[-1]
                # banco.`tabela`
                if k + 2 < len(rest) and rest[k + 1] == ("word", ".") :
                    ref = ident_of(rest[k + 2])
                    k += 2
                ref_table = ref
                out.append(q(ref))
                k += 1
                continue
            if t[0] == "group":
                out.append(self.index_cols(t[1], table)[0])
            else:
                out.append(self.tok_text(t, "ident"))
            k += 1
        if not ref_table:
            raise ValueError("FOREIGN KEY sem REFERENCES")
        sql = (f"ALTER TABLE {q(table)} ADD " + (f"CONSTRAINT {q(cname)} " if cname else "")
               + "FOREIGN KEY " + cols_sql + " " + " ".join(out) + ";")
        self.fks.append((sql, table, ref_table, ", ".join(cols)))
        self.info.setdefault(ident(table), {"fks": 0, "rows": 0})["fks"] += 1
        self.stats["fks"] += 1

    # -- INSERT -----------------------------------------------------------
    def insert(self, stmt):
        toks = tokenize(stmt)
        verb = toks[0][1].upper()
        i = 1
        ignore = False
        while i < len(toks) and is_word(toks[i], "LOW_PRIORITY", "DELAYED", "HIGH_PRIORITY", "IGNORE", "INTO"):
            ignore = ignore or toks[i][1].upper() == "IGNORE"
            i += 1
        if i >= len(toks):
            self.body.append(fragment(stmt) + ";")
            return
        tname_tok = toks[i]
        table = ident_of(tname_tok) or "?"
        if tname_tok[0] == "word" and "." in table:
            table = table.split(".")[-1]
        i += 1
        if i + 1 < len(toks) and toks[i] == ("word", "."):
            table = ident_of(toks[i + 1])
            i += 2
        cols = None
        if i < len(toks) and toks[i][0] == "group":
            cols = [ident_of(tokenize(c)[0]) for c in split_top(toks[i][1]) if tokenize(c)]
            i += 1
        if not is_word(toks[i] if i < len(toks) else None, "VALUES", "VALUE"):
            self.body.append(fragment(stmt) + ";")
            self.add(table, None, "INSERT ... SELECT", "copiado", "warn", "revise a consulta")
            self.data_tables.add(ident(table))
            return
        i += 1
        tuples = []
        while i < len(toks) and toks[i][0] == "group":
            tuples.append(toks[i][1])
            i += 1
            if i < len(toks) and toks[i] == ("punct", ","):
                i += 1
        trailing = toks[i:]

        schema = self.schema.get(ident(table))
        col_types = None
        if schema:
            types = dict(schema)
            names = [ident(c) for c in cols] if cols else [c for c, _ in schema]
            col_types = [types.get(c, "") for c in names]
        elif ident(table) not in self.schema:
            self.add(table, None, "INSERT", "INSERT", "warn",
                     "tabela não definida no script: valores booleanos/datas não foram verificados")

        zero_dates = 0
        rows = []
        for tup in tuples:
            vals = split_top(tup)
            out_vals = []
            for idx, raw in enumerate(vals):
                ctype = (col_types[idx] if col_types and idx < len(col_types) else "").upper()
                v = self.convert_value(raw, ctype)
                if v is None:
                    zero_dates += 1
                    v = "NULL"
                out_vals.append(v)
            rows.append("(" + ", ".join(out_vals) + ")")
        head = f"INSERT INTO {q(table)}" + (" (" + ", ".join(q(c) for c in cols) + ")" if cols else "")
        tail = ""
        if ignore:
            tail = " ON CONFLICT DO NOTHING"
        if any(is_word(t, "DUPLICATE") for t in trailing):
            tail = " ON CONFLICT DO NOTHING"
            self.add(table, None, "ON DUPLICATE KEY UPDATE", "ON CONFLICT DO NOTHING", "err",
                     "atualização em conflito não convertida: escreva ON CONFLICT (...) DO UPDATE")
        if verb == "REPLACE":
            self.add(table, None, "REPLACE INTO", "INSERT INTO", "warn",
                     "REPLACE não existe: linhas duplicadas causarão erro")
        self.body.append(head + " VALUES\n  " + ",\n  ".join(rows) + tail + ";")
        self.stats["rows"] += len(rows)
        self.info.setdefault(ident(table), {"fks": 0, "rows": 0})["rows"] += len(rows)
        self.data_tables.add(ident(table))
        if zero_dates:
            self.add(table, None, "'0000-00-00'", "NULL", "warn",
                     f"{zero_dates} data(s) zero substituída(s) por NULL")

    def convert_value(self, raw, ctype):
        toks = tokenize(raw)
        if len(toks) == 1:
            k, v = toks[0]
            if k in ("str", "dq"):
                if ctype == "BOOLEAN" and v in ("0", "1"):
                    return "TRUE" if v == "1" else "FALSE"
                if ctype.startswith(("TIMESTAMP", "DATE")) and ZERO_DATE.match(v):
                    return None
                return pg_str(v)
            if k == "word":
                if ctype == "BOOLEAN" and v in ("0", "1"):
                    return "TRUE" if v == "1" else "FALSE"
                if re.match(r"^0x[0-9A-Fa-f]+$", v):
                    return f"'\\x{v[2:]}'::bytea"
                return v
        if len(toks) == 2 and toks[0][0] == "word" and toks[1][0] == "str":
            pre = toks[0][1].lower()
            if pre == "b":
                if ctype == "BOOLEAN":
                    return "TRUE" if toks[1][1].strip("0") else "FALSE"
                return f"B'{toks[1][1]}'"
            if pre == "x":
                return f"'\\x{toks[1][1]}'::bytea"
            if pre.startswith("_"):
                return pg_str(toks[1][1])
        return fragment(raw)

    # -- ALTER TABLE (exportações do phpMyAdmin) ---------------------------
    def alter_table(self, stmt):
        m = re.match(r"ALTER\s+(?:IGNORE\s+)?TABLE\s*", stmt, re.I)
        rest = stmt[m.end():]
        toks = tokenize(rest)
        if not toks:
            return
        table = ident_of(toks[0])
        consumed = 1
        if len(toks) > 2 and toks[1] == ("word", "."):
            table = ident_of(toks[2])
            consumed = 3
        # posição de texto após o nome
        name_end = 0
        cnt = 0
        j = 0
        while cnt < consumed and j < len(rest):
            c = rest[j]
            if c.isspace():
                j += 1
                continue
            if c in "`\"'":
                _, j = read_quoted(rest, j)
            else:
                k = j
                while k < len(rest) and rest[k] not in _WORD_STOP:
                    k += 1
                j = k
            cnt += 1
        name_end = j
        specs = split_top(rest[name_end:])
        for spec in specs:
            st = tokenize(spec)
            if not st:
                continue
            w0 = st[0][1].upper() if st[0][0] == "word" else ""
            w1 = st[1][1].upper() if len(st) > 1 and st[1][0] == "word" else ""
            if w0 == "ADD" and w1 in ("PRIMARY",):
                grp = next((t for t in st if t[0] == "group"), ("group", ""))
                self.body.append(f"ALTER TABLE {q(table)} ADD PRIMARY KEY {self.index_cols(grp[1], table)[0]};")
                self.add(table, None, "ADD PRIMARY KEY", "ADD PRIMARY KEY", "ok", "chave primária")
            elif w0 == "ADD" and w1 == "UNIQUE":
                grp_i = next((k for k, t in enumerate(st) if t[0] == "group"), None)
                iname = None
                for t in st[2:grp_i]:
                    if not is_word(t, "KEY", "INDEX"):
                        iname = ident_of(t)
                        break
                cols_sql, cols = self.index_cols(st[grp_i][1] if grp_i is not None else "", table)
                if iname:
                    final, _ = self.unique_name(iname, table)
                    self.body.append(f"ALTER TABLE {q(table)} ADD CONSTRAINT {q(final)} UNIQUE {cols_sql};")
                else:
                    self.body.append(f"ALTER TABLE {q(table)} ADD UNIQUE {cols_sql};")
                self.add(table, ", ".join(cols), "ADD UNIQUE KEY", "ADD CONSTRAINT UNIQUE", "ok", "restrição única")
            elif w0 == "ADD" and w1 in ("KEY", "INDEX", "FULLTEXT", "SPATIAL"):
                self.table_index(st[1:], table, w1)
            elif w0 == "ADD" and w1 in ("CONSTRAINT", "FOREIGN"):
                sub = st[1:]
                cname = None
                if w1 == "CONSTRAINT":
                    if len(sub) > 1 and not is_word(sub[1], "FOREIGN", "UNIQUE", "PRIMARY", "CHECK"):
                        cname = ident_of(sub[1])
                        sub = sub[2:]
                    else:
                        sub = sub[1:]
                if sub and is_word(sub[0], "FOREIGN"):
                    self.foreign_key(sub, table, cname)
                else:
                    self.body.append(f"ALTER TABLE {q(table)} {fragment(spec, 'ident')};")
            elif w0 in ("MODIFY", "CHANGE", "ADD"):
                self.alter_column(st, table, w0)
            elif w0 == "AUTO_INCREMENT":
                val = next((t[1] for t in st if t[0] == "word" and t[1].isdigit()), None)
                if val:
                    self.auto_start[ident(table)] = int(val)
            else:
                self.body.append(f"ALTER TABLE {q(table)} {fragment(spec, 'ident')};")
                self.add(table, None, f"ALTER TABLE ... {w0}", "copiado", "warn", "revise manualmente")

    def alter_column(self, st, table, verb):
        i = 1
        if is_word(st[i] if i < len(st) else None, "COLUMN"):
            i += 1
        if verb == "CHANGE":
            old = ident_of(st[i])
            i += 1
        col_toks = st[i:]
        if verb == "ADD":
            col = self.column(col_toks, table)
            self.body.append(f"ALTER TABLE {q(table)} ADD COLUMN {col['sql']};")
            self.add(table, col["name"], col["src"], col["pg"], col["st"], col["note"])
            if ident(table) in self.schema:
                self.schema[ident(table)].append((ident(col["name"]), col["pg"]))
            return
        col = self.column(col_toks, table)
        name = col["name"]
        stmts = []
        if verb == "CHANGE" and old != name:
            stmts.append(f"ALTER TABLE {q(table)} RENAME COLUMN {q(old)} TO {q(name)};")
        if col["serial"]:
            # phpMyAdmin define AUTO_INCREMENT via MODIFY depois de criar a tabela
            stmts.append(f"ALTER TABLE {q(table)} ALTER COLUMN {q(name)} SET NOT NULL;")
            stmts.append(f"ALTER TABLE {q(table)} ALTER COLUMN {q(name)} ADD GENERATED BY DEFAULT AS IDENTITY;")
            self.serial_cols[ident(table)] = name
            self.add(table, name, "MODIFY ... AUTO_INCREMENT", "GENERATED BY DEFAULT AS IDENTITY", "ok",
                     "coluna identidade (equivalente moderno ao SERIAL)")
        else:
            pg = col["pg"]
            stmts.append(f"ALTER TABLE {q(table)} ALTER COLUMN {q(name)} TYPE {pg} USING {q(name)}::{pg};")
            if col["not_null"] is True:
                stmts.append(f"ALTER TABLE {q(table)} ALTER COLUMN {q(name)} SET NOT NULL;")
            elif col["not_null"] is False:
                stmts.append(f"ALTER TABLE {q(table)} ALTER COLUMN {q(name)} DROP NOT NULL;")
            if col["default"]:
                stmts.append(f"ALTER TABLE {q(table)} ALTER COLUMN {q(name)} SET DEFAULT {col['default']};")
            self.add(table, name, f"{verb} {col['src']}", col["pg"], "warn",
                     "alteração de coluna dividida em ALTER COLUMN; confira")
        self.body.extend(stmts)

    # -- outras instruções ---------------------------------------------------
    def drop_table(self, stmt):
        m = re.match(r"DROP\s+(?:TEMPORARY\s+)?TABLE\s*(IF\s+EXISTS\s+)?(.*)$", stmt, re.I | re.S)
        names = []
        for part in split_top(m.group(2)):
            toks = [t for t in tokenize(part) if t[0] in ("bq", "dq", "word") and t[1] != "."]
            if toks and not is_word(toks[-1], "CASCADE", "RESTRICT"):
                names.append(toks[-1][1].split(".")[-1])
        if names:
            self.drops.append("DROP TABLE " + ("IF EXISTS " if m.group(1) else "")
                              + ", ".join(q(n) for n in names) + " CASCADE;")

    def statement(self, stmt, kind):
        """Converte uma instrução; se ela estiver malformada, registra erro e segue."""
        snapshot = (len(self.drops), len(self.tables), len(self.body), len(self.indexes),
                    len(self.fks), len(self.comments), dict(self.stats), len(self.log))
        try:
            self._statement(stmt, kind)
        except Exception:
            (self.drops[snapshot[0]:], self.tables[snapshot[1]:], self.body[snapshot[2]:],
             self.indexes[snapshot[3]:], self.fks[snapshot[4]:], self.comments[snapshot[5]:]) = [], [], [], [], [], []
            self.stats = snapshot[6]
            del self.log[snapshot[7]:]
            resumo = " ".join(stmt.split())[:60]
            self.body.append("-- NÃO CONVERTIDO (sintaxe não reconhecida): " + resumo + "...")
            self.add(None, None, resumo, "não convertido", "err",
                     "instrução incompleta ou com sintaxe não reconhecida; revise o SQL de origem")

    def _statement(self, stmt, kind):
        head = stmt.lstrip()[:40].upper()
        if kind == "routine":
            self.add(None, None, "PROCEDURE/FUNCTION/TRIGGER", "não convertido", "err",
                     "rotinas precisam ser reescritas em PL/pgSQL")
            return
        if re.match(r"CREATE\s+(TEMPORARY\s+)?TABLE\b", head):
            self.create_table(stmt)
        elif re.match(r"(INSERT|REPLACE)\b", head):
            if self.include_data:
                self.insert(stmt)
            else:
                self.stats["skipped_inserts"] += 1
        elif re.match(r"DROP\s+(TEMPORARY\s+)?TABLE\b", head):
            self.drop_table(stmt)
        elif re.match(r"ALTER\s+(IGNORE\s+)?TABLE\b", head):
            self.alter_table(stmt)
        elif re.match(r"(SET|LOCK|UNLOCK|USE)\b", head):
            return  # diretivas de sessão do MySQL
        elif re.match(r"CREATE\s+(DATABASE|SCHEMA)\b", head):
            self.add(None, None, "CREATE DATABASE", "removido", "ok",
                     "crie o banco separadamente e conecte-se a ele antes de executar")
        elif re.match(r"(START\s+TRANSACTION|BEGIN|COMMIT|ROLLBACK)\b", head):
            return
        elif re.match(r"CREATE\s+(UNIQUE\s+)?INDEX\b", head):
            sql = re.sub(r"\s+USING\s+BTREE\b", "", fragment(stmt, "ident"), flags=re.I)
            self.indexes.append(sql + ";")
            self.stats["indexes"] += 1
        else:
            self.body.append(fragment(stmt) + ";")
            kw = " ".join(stmt.split()[:2]).upper()
            self.add(None, None, kw, "copiado", "warn", "instrução não convertida automaticamente; revise")

    # -- montagem final ----------------------------------------------------
    def run(self, sql):
        for stmt, kind in split_statements(sql):
            self.statement(stmt, kind)

        sequences = []
        for table, col in self.serial_cols.items():
            start = self.auto_start.get(table)
            if table in self.data_tables or (start and start > 1):
                nxt = f"COALESCE((SELECT MAX({q(col)}) FROM {q(table)}), 0) + 1"
                if start and start > 1:
                    nxt = f"GREATEST({nxt}, {start})"
                sequences.append(f"SELECT setval(pg_get_serial_sequence({pg_str(q(table))}, "
                                 f"{pg_str(ident(col))}), {nxt}, false);")
                self.add(table, col, f"AUTO_INCREMENT{'=' + str(start) if start else ''}", "setval(...)",
                         "ok", "sequência ajustada para continuar após o maior id")

        defined = set(self.schema)
        fk_sql = []
        for sql_fk, table, ref, cols in self.fks:
            fk_sql.append(sql_fk)
            if ref and ident(ref) not in defined:
                self.add(table, cols, f"FOREIGN KEY -> {ref}", "ALTER TABLE ADD CONSTRAINT", "warn",
                         f"tabela referenciada '{ref}' não está no script; crie-a antes")
            else:
                self.add(table, cols, f"FOREIGN KEY -> {ref}", "ALTER TABLE ADD CONSTRAINT", "ok",
                         "criada no final do script (independe da ordem das tabelas)")

        if self.stats["skipped_inserts"]:
            self.add(None, None, f"{self.stats['skipped_inserts']} INSERT", "ignorado", "ok",
                     "modo somente estrutura (DDL)")

        sections = [
            ("Remoção de tabelas existentes", self.drops),
            ("Tabelas", self.tables),
            ("Comentários", self.comments),
            ("Dados e alterações", self.body),
            ("Índices", self.indexes),
            ("Sequências", sequences),
            ("Chaves estrangeiras", fk_sql),
        ]
        out = []
        for title, items in sections:
            if items:
                out.append(f"-- {title}\n" + "\n\n".join(items) if title in ("Tabelas",)
                           else f"-- {title}\n" + "\n".join(items))
        self.stats["warn"] = sum(1 for x in self.log if x["st"] == "warn")
        self.stats["err"] = sum(1 for x in self.log if x["st"] == "err")
        return "\n\n".join(out)


def convert(sql, include_data=True):
    """Converte um script MySQL. Devolve dict(sql, log, stats, tables)."""
    sql = (sql or "").strip()
    if not sql:
        raise ValueError("O SQL informado está vazio.")
    c = Converter(include_data=include_data)
    out = c.run(sql)
    if not out.strip():
        raise ValueError("Nenhuma instrução conversível foi encontrada no SQL informado.")
    table_info = [{"name": t, "cols": len(cols), "fks": c.info.get(t, {}).get("fks", 0),
                   "rows": c.info.get(t, {}).get("rows", 0)} for t, cols in c.schema.items()]
    return {"sql": out, "log": c.log, "stats": c.stats, "tables": list(c.schema), "table_info": table_info}
