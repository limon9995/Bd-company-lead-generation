(function(){
  const $ = (s, r=document) => r.querySelector(s);
  const $$ = (s, r=document) => Array.from(r.querySelectorAll(s));
  const csrf = () => $('meta[name=csrf]').content;

  async function postJSON(url, body){
    const r = await fetch(url, {method:'POST', headers:{'X-CSRF-Token':csrf()}, body});
    try { return await r.json(); } catch(e){ return {ok:false, message:'HTTP '+r.status}; }
  }

  // Theme toggle (light / dark / system), remembered per browser.
  function applyTheme(t){
    if(t === 'light' || t === 'dark') document.documentElement.setAttribute('data-theme', t);
    else document.documentElement.removeAttribute('data-theme');
    $$('[data-theme-label]').forEach(el => el.textContent = t === 'dark' ? 'Dark' : t === 'light' ? 'Light' : 'System');
  }
  let theme = 'system';
  try { theme = localStorage.getItem('theme') || 'system'; } catch(e){}
  applyTheme(theme);
  document.addEventListener('click', e => {
    const t = e.target.closest('[data-theme-toggle]'); if(!t) return;
    theme = theme === 'system' ? 'dark' : theme === 'dark' ? 'light' : 'system';
    try { localStorage.setItem('theme', theme); } catch(e){}
    applyTheme(theme);
  });

  // Mobile nav
  document.addEventListener('click', e => {
    if(e.target.closest('[data-nav-open]')) document.body.classList.add('nav-open');
    if(e.target.closest('.scrim') || e.target.closest('[data-nav-close]')) document.body.classList.remove('nav-open');
  });

  // Toasts auto-hide (errors stay until closed)
  $$('.toast').forEach(t => {
    t.querySelector('button')?.addEventListener('click', () => t.remove());
    if(!t.classList.contains('err')) setTimeout(() => { t.style.opacity = '0'; setTimeout(() => t.remove(), 350); }, 5000);
  });

  // Buttons that POST and show a JSON {ok, message} result inline (Settings → Test / Detect)
  document.addEventListener('click', async e => {
    const b = e.target.closest('[data-post-json]'); if(!b) return;
    e.preventDefault();
    const out = document.getElementById(b.dataset.out);
    out.className = 'test-result'; out.textContent = 'Working…'; b.disabled = true;
    const res = await postJSON(b.dataset.postJson);
    b.disabled = false;
    out.className = 'test-result ' + (res.ok ? 'ok' : 'err');
    out.textContent = (res.ok ? '✔ ' : '✖ ') + res.message;
    if(res.ok && b.dataset.reload) setTimeout(() => location.reload(), 1200);
  });

  // Confirm before destructive submits
  document.addEventListener('submit', e => {
    const f = e.target, btn = e.submitter;
    const msg = (btn && btn.dataset.confirm) || f.dataset.confirm;
    if(msg && !confirm(msg)) e.preventDefault();
  });

  // Leads: select all + bulk bar
  const bulk = $('#bulk-form');
  if(bulk){
    const boxes = () => $$('input[name=ids]', bulk);
    const update = () => {
      const n = boxes().filter(b => b.checked).length;
      $('#bulk-count').textContent = n;
      $('#bulkbar').hidden = n === 0;
    };
    $('#select-all')?.addEventListener('change', e => { boxes().forEach(b => b.checked = e.target.checked); update(); });
    bulk.addEventListener('change', e => { if(e.target.name === 'ids') update(); });
    update();
  }

  // Dashboard chart tooltip
  $$('.chart-box').forEach(box => {
    const tip = $('.chart-tip', box);
    $$('.hit', box).forEach(h => {
      h.addEventListener('mouseenter', () => {
        const svg = $('svg', box), r = svg.getBoundingClientRect(), vb = svg.viewBox.baseVal;
        tip.innerHTML = h.dataset.tip;
        const frac = (+h.dataset.cx) / vb.width;
        tip.style.left = (frac * r.width) + 'px';
        // keep the tooltip inside the card near the edges
        tip.style.transform = frac > 0.8 ? 'translate(-100%,-110%)' : frac < 0.2 ? 'translate(0,-110%)' : 'translate(-50%,-110%)';
        tip.style.top = ((+h.dataset.cy) / vb.height * r.height) + 'px';
        tip.style.opacity = 1;
        $('#' + h.dataset.bar, box)?.classList.add('hot');
      });
      h.addEventListener('mouseleave', () => { tip.style.opacity = 0; $('#' + h.dataset.bar, box)?.classList.remove('hot'); });
    });
  });

  // Run page: live progress while running
  const live = $('[data-run-status]');
  if(live){
    const url = live.dataset.runStatus;
    const tick = async () => {
      let d; try { d = await (await fetch(url)).json(); } catch(e){ return setTimeout(tick, 5000); }
      $('#run-bar').style.width = d.percent + '%';
      $('#run-pct').textContent = d.percent + '%';
      $('#run-jobs').textContent = d.finished + ' / ' + d.total + ' steps';
      for(const [k, v] of Object.entries(d.counters)){ const el = document.getElementById('c-' + k); if(el) el.textContent = v; }
      if(d.status !== 'running') location.reload(); else setTimeout(tick, 3000);
    };
    setTimeout(tick, 2000);
  }

  // Template editor: live preview
  const tf = $('#template-form');
  if(tf){
    let timer;
    const run = async () => {
      const res = await postJSON('/templates/preview', new FormData(tf));
      $('#pv-subject').textContent = res.subject || '(no subject)';
      $('#pv-body').textContent = res.body || '';
      $('#pv-sample').textContent = res.sample || '';
      const warn = $('#pv-unknown');
      warn.hidden = !(res.unknown_vars && res.unknown_vars.length);
      if(!warn.hidden) warn.textContent = 'Unknown variables (will be blank): ' + res.unknown_vars.map(v => '{{' + v + '}}').join(', ');
    };
    tf.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(run, 400); });
    $$('[data-insert-var]').forEach(b => b.addEventListener('click', () => {
      const ta = $('textarea[name=body_tpl]', tf), v = '{{' + b.dataset.insertVar + '}}';
      const s = ta.selectionStart ?? ta.value.length;
      ta.value = ta.value.slice(0, s) + v + ta.value.slice(ta.selectionEnd ?? s);
      ta.focus(); ta.selectionStart = ta.selectionEnd = s + v.length; run();
    }));
    run();
  }
})();
