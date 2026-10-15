# DBConverter — MySQL/MariaDB → PostgreSQL

Conversor web de banco de dados com login de usuários, histórico de conversões
e relatório em PDF/HTML.

## Como rodar (VS Code + Live Server) — recomendado

Não precisa de Python nem de instalar nada além da extensão.

1. Extraia o ZIP e abra a pasta `tcc_final` no VS Code (**Arquivo → Abrir Pasta**).
2. Instale a extensão **Live Server** (de Ritwick Dey), se ainda não tiver.
3. Clique com o botão direito em `index.html` → **Open with Live Server**.
4. Na tela inicial:
   - **Entrar** com a conta de demonstração `admin` / `demo123`, ou
   - **Criar conta** com nome, usuário e senha (mín. 6 caracteres, com letras e números).

Nesse modo, contas e histórico ficam salvos **no navegador** (localStorage).
As senhas são guardadas como hash PBKDF2‑SHA256 com salt, nunca em texto puro.
Outro navegador ou outro computador começa sem dados.

> Abra pelo Live Server (endereço `http://127.0.0.1:5500`). Se abrir o
> `index.html` com dois cliques (`file://`), alguns navegadores bloqueiam a
> criptografia usada no login.

## Usando o sistema

1. **Nova Conversão → Etapa 1**: cole um script MySQL (ou clique em
   **Abrir arquivo .sql**) e depois em **Analisar SQL**. Também dá para clicar
   em **Testar Conexão** para usar o banco de exemplo `loja_ecommerce`.
2. **Etapa 2**: escolha as tabelas e o modo: *somente estrutura* (DDL) ou
   *estrutura + dados* (DDL + INSERTs).
3. **Etapa 3**: veja o script PostgreSQL, o log de alterações e o comparativo.
   Em **Salvar relatório da conversão**, baixe:
   - **Relatório PDF**: identificação, resumo, log completo e scripts;
   - **Relatório HTML**: o mesmo conteúdo, num arquivo que abre em qualquer navegador;
   - **Script .sql**: só o SQL convertido.
4. **Histórico e Relatórios**: cada usuário vê apenas as próprias conversões e
   pode reabrir, baixar o relatório de novo ou excluir.

## O que o conversor faz

Aceita `CREATE TABLE`, `INSERT`, `ALTER TABLE` e exportações completas do
**mysqldump** e do **phpMyAdmin**. A página **Mapeamentos** lista todas as regras.
Destaques:

- `TINYINT(1)` → `BOOLEAN`, incluindo `DEFAULT 1` → `DEFAULT TRUE` e valores dos INSERTs;
- `INT(11) AUTO_INCREMENT` → `SERIAL`, e a sequência continua após o maior id (`setval`);
- `ENUM` → `VARCHAR(n) + CHECK`; `UNSIGNED` → `CHECK (col >= 0)`;
- `KEY`/`INDEX` → `CREATE INDEX`; nomes repetidos entre tabelas são renomeados;
- chaves estrangeiras criadas no final do script, então ele roda mesmo que as
  tabelas estejam fora de ordem;
- datas `'0000-00-00'` → `NULL`; strings `'It\'s'` → `'It''s'`;
- procedures e triggers são marcados como **Erro** no log, para reescrita manual.

Os scripts gerados foram testados em um **PostgreSQL 16** real.

## Modo alternativo: backend Flask (opcional)

Use se quiser os dados num banco de servidor em vez do navegador. Precisa de
Python 3.10+. No terminal do VS Code, dentro da pasta `tcc_final`:

**Windows (PowerShell)**

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

**Linux/macOS**

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Abra `http://127.0.0.1:5000`. A tela é a mesma; o rodapé do login indica
"Conectado ao servidor Flask". Localmente o Flask usa SQLite (`data/app.db`) e
cria a conta `admin` / `demo123`. Para PostgreSQL, defina `DATABASE_URL`
(veja `.env.example`).

## Arquivos

| Arquivo | Função |
|---|---|
| `index.html` | Interface única (Live Server e Flask) |
| `static/converter.js` | Motor de conversão no navegador |
| `converter.py` | O mesmo motor em Python (usado pelo Flask) |
| `static/backend.js` | Login, cadastro e histórico (navegador ou API Flask) |
| `static/report.js` | Geração dos relatórios PDF e HTML |
| `static/demo-data.js` | Banco de exemplo e tabela de mapeamentos |
| `static/app.js` / `static/style.css` | Comportamento e visual da interface |
| `static/vendor/` | Biblioteca jsPDF (local, funciona sem internet) |
| `app.py` | Backend Flask opcional |

## Limitações

- O navegador não consegue se conectar a um MySQL real. Por isso **Testar
  Conexão** usa um banco de demonstração. Para converter um banco real, exporte-o
  (mysqldump ou phpMyAdmin → Exportar → SQL) e cole ou abra o arquivo na Etapa 1.
- No modo Live Server, os dados ficam só naquele navegador. Limpar os dados do
  site apaga contas e histórico. Baixe os relatórios que quiser guardar.
- Procedures, functions, triggers e `ON DUPLICATE KEY UPDATE` precisam de
  ajuste manual (aparecem como Erro no log).
- Revise o SQL convertido antes de executá-lo em produção.
