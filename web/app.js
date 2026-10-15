'use strict';
let csrf = '';
const el = id => document.getElementById(id);
const node = (tag, text) => { const n = document.createElement(tag); n.textContent = text; return n; };
async function api(path, data) {
  const response = await fetch(path, {method: data === undefined ? 'GET' : 'POST',
    headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf},
    body: data === undefined ? undefined : JSON.stringify(data)});
  const result = await response.json();
  if (!response.ok) { if (response.status === 401) show(false); throw Error(result.error || 'Richiesta fallita'); }
  return result;
}
function show(connected) {
  el('login').hidden = connected; el('dashboard').hidden = !connected; el('logout').hidden = !connected;
  if (!connected) {el('config-fields').replaceChildren(); el('config-save').hidden = true; configRevision = '';}
}
let configRevision = '';
el('config-load').onclick = async () => {
  try {
    const result = await api('/api/configuration');
    configRevision = result.revision;
    el('config-info').textContent = result.apply;
    el('config-fields').replaceChildren();
    const groups = new Map();
    result.fields.forEach(field => {
      if (!groups.has(field.group)) {
        const details = node('details', ''); details.append(node('summary', field.group));
        groups.set(field.group, details); el('config-fields').append(details);
      }
      const label = node('label', field.key);
      const input = node(field.key === 'GCAL_SERVICE_ACCOUNT_JSON' ? 'textarea' : 'input', '');
      if (input.tagName === 'INPUT') input.type = field.secret ? 'password' : 'text';
      input.autocomplete = 'off'; input.spellcheck = false;
      input.dataset.key = field.key; input.dataset.original = field.secret ? '' : (field.value || '');
      input.dataset.secret = String(field.secret);
      input.value = field.secret ? '' : (field.value || '');
      input.disabled = !!field.readonly;
      input.placeholder = field.secret ? (field.configured ? 'Configurato · vuoto mantiene il segreto' : 'Non configurato') : 'Non impostato (default applicazione)';
      label.append(input);
      if (field.readonly) label.append(node('small', field.readonly));
      groups.get(field.group).append(label);
    });
    el('config-save').hidden = false; el('config-status').textContent = 'Modificare solo i campi necessari. Le verifiche di rete sono senza invii o pairing; OAuth e sessioni richiedono il terminale.';
  } catch(e) {el('config-status').textContent = e.message;}
};
el('config-form').onsubmit = async event => {
  event.preventDefault();
  const changes = {};
  const inputs = [...el('config-fields').querySelectorAll('input,textarea')];
  inputs.filter(input => !input.disabled).forEach(input => {
    if (input.value !== input.dataset.original && (input.dataset.secret !== 'true' || input.value)) changes[input.dataset.key] = input.value;
  });
  if (!Object.keys(changes).length) {el('config-status').textContent = 'Nessuna modifica.'; return;}
  if (!confirm('Verificare i candidati tramite rete e salvare il batch per il prossimo avvio? Nessun riavvio o migrazione automatica.')) return;
  el('config-save').disabled = true; el('config-load').disabled = true;
  el('config-status').textContent = 'Verifica in corso… nessuna modifica live.';
  try {
    await api('/api/configuration', {revision: configRevision, changes});
    // A successful revision is deliberately invalidated: reload before another edit.
    el('config-fields').replaceChildren(); el('config-save').hidden = true; configRevision = '';
    el('config-status').textContent = 'Batch salvato atomicamente. Runtime invariato: riavviare manualmente quando opportuno. Nessuna migrazione DB eseguita.';
  } catch(e) {el('config-status').textContent = e.message;}
  finally {
    inputs.filter(input => input.dataset.secret === 'true').forEach(input => {input.value = '';});
    Object.keys(changes).forEach(key => {delete changes[key];});
    el('config-save').disabled = false; el('config-load').disabled = false;
  }
};
let filePath = '';
const fileQuery = path => new URLSearchParams({root: el('file-root').value, path}).toString();
async function browseFiles() {
  try {
    const result = await api(`/api/files?${fileQuery(filePath)}`);
    el('file-path').textContent = `${el('file-root').selectedOptions[0].textContent} / ${filePath}`;
    el('file-up').disabled = !filePath;
    el('file-list').replaceChildren();
    result.entries.forEach(entry => {
      const path = filePath ? `${filePath}/${entry.name}` : entry.name;
      const r = row(el('file-list'), entry.name, entry.directory ? 'Cartella' : `${entry.size} byte`);
      if (entry.directory) {
        const b = node('button', 'Apri'); b.onclick = () => {filePath = path; browseFiles();}; r.append(b);
      } else {
        const a = node('a', 'Scarica'); a.href = `/api/files/download?${fileQuery(path)}`; r.append(a);
        const b = node('button', 'Elimina');
        b.onclick = async () => {
          if (!confirm(`Eliminare ${entry.name}?`)) return;
          try {
            const response = await fetch(`/api/files?${fileQuery(path)}`, {method:'DELETE', headers:{'X-CSRF-Token':csrf}});
            const result = await response.json();
            if (!response.ok) throw Error(result.error || 'Eliminazione fallita');
            await browseFiles();
          } catch(e) {el('file-status').textContent = e.message;}
        }; r.append(b);
      }
    });
    el('file-status').textContent = '';
  } catch(e) {el('file-status').textContent = e.message;}
}
el('file-root').onchange = () => {filePath = ''; browseFiles();};
el('file-up').onclick = () => {filePath = filePath.split('/').slice(0,-1).join('/'); browseFiles();};
el('file-refresh').onclick = browseFiles;
el('file-send').onclick = async () => {
  const file = el('file-upload').files[0];
  if (!file) return;
  if (file.size > 20*1024*1024) {el('file-status').textContent = 'File troppo grande'; return;}
  el('file-send').disabled = true;
  try {
    const response = await fetch(`/api/files/upload?${new URLSearchParams({name:file.name})}`, {
      method:'POST', headers:{'X-CSRF-Token':csrf, 'Content-Type':'application/octet-stream'}, body:file});
    const result = await response.json();
    if (!response.ok) throw Error(result.error || 'Upload fallito');
    el('file-root').value = 'data'; filePath = 'uploads'; el('file-upload').value = ''; await browseFiles();
    el('file-status').textContent = 'File caricato';
  } catch(e) {el('file-status').textContent = e.message;}
  finally {el('file-send').disabled = false;}
};
function row(parent, left, right) { const r = node('div', ''); r.className = 'row'; r.append(node('div', left), node('small', right)); parent.append(r); return r; }
async function refresh() {
  try {
    const d = await api('/api/dashboard'); show(true);
    el('status').textContent = `● ${d.agent_name} · Collegato · ${d.timezone} · ultimo aggiornamento ${new Date().toLocaleTimeString('it')}`;
    const today = d.today;
    const sum = (direction) => d.counts.filter(r => String(r[0]).slice(0,10) === today && r[2] === direction).reduce((s,r)=>s+r[3],0);
    el('kpis').replaceChildren();
    [['In entrata oggi',sum('in')],['In uscita oggi',sum('out')],['Da gestire · ieri/oggi',d.counts.reduce((s,r)=>s+r[5],0)],['Appuntamenti aperti',d.appointments]].forEach(([label,value])=>{
      const c=node('div','');c.className='card';c.append(node('small',label),node('strong',value));el('kpis').append(c);
    });
    el('flows').replaceChildren();
    d.flows.forEach(f=>{const h=f.health;const age=h.observed_at?Math.floor(Date.now()/1000-h.observed_at):null;
      const r=row(el('flows'),f.label,`${h.status} ${f.configuration?`· configurazione: ${f.configuration}`:''} ${age===null?'':`· ${age}s fa`}`);
      const b=node('button',f.enabled?'Flusso abilitato · sospendi':'Flusso sospeso · abilita');b.setAttribute('aria-label',`${f.label}: ${b.textContent}`);
      b.onclick=async()=>{b.disabled=true;try{await api(`/api/flows/${f.id}`,{enabled:!f.enabled});await refresh();}catch(e){el('status').textContent=e.message;}finally{b.disabled=false;}};r.append(b);
    });
    el('traffic').replaceChildren();d.counts.forEach(r=>row(el('traffic'),`${r[0]} · ${r[1]} · ${r[2]}`,`${r[3]} messaggi / ${r[4]} analizzati`));
    el('activity').replaceChildren();d.recent.forEach(r=>row(el('activity'),`${r[0]} · ${r[1]||'direzione ignota'}`,`${r[3]} · ${r[2]||'data ignota'}`));
    (d.jobs||[]).forEach(j=>row(el('activity'),`${j.kind} · ${j.status}: ${j.count}`,`età massima ${Math.round(j.oldest_age_seconds||0)}s · ultimo successo ${j.last_success||'mai'}`));
    row(el('activity'),'Notifiche confermate',String(d.notifications_sent||0));
    (d.calendar_pending||[]).forEach(a=>row(el('activity'),`Calendario #${a[0]} ${a[1]}`,`${a[2]} · ${a[3]}`));
    el('invoices').replaceChildren();d.invoices.forEach(r=>row(el('invoices'),`${r[0]} · ${r[1]}`,`${r[2]} documenti`));
    el('zips').replaceChildren();d.zips.forEach(r=>row(el('zips'),`ZIP ${r[0]} · ${r[1]}`,r[2]?'inviato':'non inviato'));
  } catch(e) {el('status').textContent=e.message;}
}
el('login').onsubmit=async e=>{e.preventDefault();el('error').textContent='';try{const data=Object.fromEntries(new FormData(e.target));const result=await api('/api/login',data);csrf=result.csrf;e.target.reset();show(true);await refresh();await browseFiles();}catch(err){el('error').textContent=err.message;}};
el('logout').onclick=async()=>{await api('/api/logout',{});csrf='';show(false);};
api('/api/session').then(s=>{csrf=s.csrf;show(true);refresh();browseFiles();}).catch(()=>{});
setInterval(()=>{if(!el('dashboard').hidden)refresh();},10000);