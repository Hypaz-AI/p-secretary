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
COPY data/package.json data/package-lock.json ./node/
RUN cd node && npm ci --omit=dev && npm cache clean --force
COPY actions.py admin_config.py brain.py config.py dashboard.py db.py digest.py documents.py email_attachments.py file_browser.py flows.py invoices.py jobs.py main.py message_store.py pipeline.py recovery.py installer.py ./
COPY ai_budget.py platform_health.py historical_import.py setup_gcal.py setup_ms_auth.py setup_telegram.py setup_telegram_account.py setup_whatsapp.py ./
COPY setup_ui.py ./
COPY task_reconciliation.py ./
COPY owner_dialogue.py owner_commitments.py ./
COPY policy.py capabilities.py knowledge.py secretary.py secretary_runtime.py ./
COPY execution.py ./
COPY connectors/ ./connectors/
COPY web/ ./web/
COPY docker/entrypoint.py ./docker/entrypoint.py
USER 1000:1000
ENTRYPOINT ["python", "/opt/pi-secretary/docker/entrypoint.py"]
CMD ["python", "main.py"]