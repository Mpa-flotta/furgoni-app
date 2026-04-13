APP FURGONI - VERSIONE PRONTA PER USO REALE

1) INSTALLAZIONE LOCALE
- Apri il terminale nella cartella del progetto
- Esegui:
  py -m venv .venv
  .venv\Scripts\activate
  py -m pip install -r requirements.txt

2) PASSWORD ADMIN
- Esegui:
  py genera_password_hash.py
- Copia l'hash generato
- Crea un file chiamato .env usando .env.example come modello

3) AVVIO LOCALE
Su Windows PowerShell:
  $env:SECRET_KEY="metti_una_chiave_lunga"
  $env:ADMIN_USERNAME="admin"
  $env:ADMIN_PASSWORD_HASH="incolla_hash"
  $env:APP_BASE_URL="http://127.0.0.1:5000"
  py app.py

4) ACCESSO
- Dashboard admin: /login
- User default se non imposti variabili: admin
- Password default se non imposti variabili: admin12345
  Cambiala subito prima di pubblicare online.

5) MESSA ONLINE
Puoi pubblicarla su un hosting Python come Render, Railway, un VPS o un server aziendale.
Comando consigliato di avvio:
  gunicorn -w 2 -b 0.0.0.0:$PORT wsgi:application

6) VARIABILI DA IMPOSTARE ONLINE
- SECRET_KEY
- ADMIN_USERNAME
- ADMIN_PASSWORD_HASH
- APP_BASE_URL
- DATA_DIR

7) NOTE IMPORTANTI
- Questa versione usa SQLite. Va bene per partire.
- Per uso intenso o multi-sede, meglio PostgreSQL.
- Le foto vengono salvate nella cartella uploads dentro DATA_DIR.
- Se il servizio online non ha disco persistente, foto e database non resteranno salvati dopo il riavvio.
