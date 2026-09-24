function csrf(){return document.querySelector('meta[name=csrf]').content}
async function postJSON(url){
  const r = await fetch(url,{method:'POST',headers:{'X-CSRF-Token':csrf()}});
  try{return await r.json()}catch(e){return {ok:false,message:'HTTP '+r.status}}
}
document.addEventListener('click', async (e)=>{
  const b = e.target.closest('[data-post-json]'); if(!b) return;
  e.preventDefault();
  const out = document.getElementById(b.dataset.out);
  out.className='test-result'; out.textContent='Working…'; b.disabled=true;
  const res = await postJSON(b.dataset.postJson);
  b.disabled=false; out.className='test-result '+(res.ok?'ok':'err'); out.textContent=(res.ok?'✔ ':'✖ ')+res.message;
  if(res.ok && b.dataset.reload){setTimeout(()=>location.reload(),1200)}
});
document.addEventListener('submit',(e)=>{const f=e.target; if(f.dataset.confirm && !confirm(f.dataset.confirm)) e.preventDefault();});
