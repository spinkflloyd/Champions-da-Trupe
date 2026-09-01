# Bolão Champions League — publicação na internet

Este projeto funciona localmente com SQLite e, quando `DATABASE_URL` estiver definida, usa PostgreSQL (ideal para Render + Supabase).

## Render
- Build: `pip install -r requirements.txt`
- Start: `gunicorn app:app`
- Variáveis: `DATABASE_URL` e `FOOTBALL_DATA_TOKEN`

## Supabase
No painel do projeto, clique em **Connect** e copie a URI do **Shared Pooler / Session mode**. Ela será usada como `DATABASE_URL` no Render.

Nunca publique a senha do banco ou o token da API no GitHub.
