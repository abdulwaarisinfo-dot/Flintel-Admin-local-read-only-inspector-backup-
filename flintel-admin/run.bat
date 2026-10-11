@echo off
if not exist .venv ( py -m venv .venv )
call .venv\Scripts\activate
pip install -q -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000
