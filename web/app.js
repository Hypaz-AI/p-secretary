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
function show(connected) { el('login').hidden = connected; el('dashboard').hidden = !connected; el('logout').hidden = !connected; }
function row(parent, left, right) { const r = node('div', ''); r.className = 'row'; r.append(node('div', left), node('small', right)); parent.append(r); return r; }
async function refresh() {
  try {
    const d = await api('/api/dashboard'); show(true);
    el('status').textContent = `● Collegato · ${d.timezone} · ultimo aggiornamento ${new Date().toLocaleTimeString('it')}`;
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
    el('invoices').replaceChildren();d.invoices.forEach(r=>row(el('invoices'),`${r[0]} · ${r[1]}`,`${r[2]} documenti`));
    el('zips').replaceChildren();d.zips.forEach(r=>row(el('zips'),`ZIP ${r[0]} · ${r[1]}`,r[2]?'inviato':'non inviato'));
  } catch(e) {el('status').textContent=e.message;}
}
el('login').onsubmit=async e=>{e.preventDefault();el('error').textContent='';try{const data=Object.fromEntries(new FormData(e.target));const result=await api('/api/login',data);csrf=result.csrf;e.target.reset();await refresh();}catch(err){el('error').textContent=err.message;}};
el('logout').onclick=async()=>{await api('/api/logout',{});csrf='';show(false);};
api('/api/session').then(s=>{csrf=s.csrf;refresh();}).catch(()=>{});
setInterval(()=>{if(!el('dashboard').hidden)refresh();},10000);