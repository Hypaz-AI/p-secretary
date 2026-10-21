FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    PUPPETEER_SKIP_DOWNLOAD=true NODE_PATH=/opt/pi-secretary/node/node_modules \
    HOME=/var/lib/pi-secretary/home XDG_CACHE_HOME=/tmp/cache \
    WA_CHROMIUM_PATH=/usr/bin/chromium
RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm chromium ca-certificates \
    && node -e 'if(Number(process.versions.node.split(".")[0])<18) process.exit(1)' \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 1000 secretary && useradd -u 1000 -g 1000 -M secretary
WORKDIR /opt/pi-secretary
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
# Runtime dell'agente incorporato nell'immagine: nessun download all'avvio (la cache XDG è una tmpfs)
# e nessun accesso di rete del runtime oltre al gateway locale verso OpenRouter.
# Il client dell'SDK avvia `copilot-runtime` (ensure_runtime_wrapper) e non scrive nulla se il
# bundle e' completo: l'alias `copilot` serve solo a get_or_download_cli(). Lo creiamo comunque in
# build (difesa in profondita': la cartella e' di root e l'app gira come utente 1000), e la build
# FALLISCE se il runtime non e' risolvibile senza rete, come a runtime.
RUN COPILOT_CLI_EXTRACT_DIR=/opt/copilot-runtime python -m copilot download-runtime \
    && COPILOT_CLI_EXTRACT_DIR=/opt/copilot-runtime python -c "from copilot._cli_download import download_cli; print(download_cli())" \
    && chmod -R a+rX /opt/copilot-runtime \
    && COPILOT_CLI_EXTRACT_DIR=/opt/copilot-runtime COPILOT_SKIP_CLI_DOWNLOAD=1 python -c "import os; from copilot._cli_download import get_or_download_cli as g; p=g(); assert p and os.access(p, os.X_OK), 'runtime non risolvibile'; print('runtime ok:', p)"
ENV COPILOT_CLI_EXTRACT_DIR=/opt/copilot-runtime COPILOT_SKIP_CLI_DOWNLOAD=1
COPY data/package.json data/package-lock.json ./node/
RUN cd node && npm ci --omit=dev && npm cache clean --force
COPY actions.py admin_config.py config.py dashboard.py dashboard_owner.py db.py digest.py documents.py email_attachments.py file_browser.py flows.py invoices.py jobs.py main.py message_store.py pipeline.py recovery.py installer.py ./
COPY ai_budget.py platform_health.py historical_import.py setup_gcal.py setup_ms_auth.py setup_telegram.py setup_telegram_account.py setup_whatsapp.py ./
COPY setup_ui.py ./
COPY task_reconciliation.py owner_commitments.py ./
COPY policy.py capabilities.py knowledge.py privacy.py ./
COPY agent/ ./agent/
COPY connectors/ ./connectors/
COPY web/ ./web/
COPY docker/entrypoint.py ./docker/entrypoint.py
USER 1000:1000
ENTRYPOINT ["python", "/opt/pi-secretary/docker/entrypoint.py"]
CMD ["python", "main.py"]