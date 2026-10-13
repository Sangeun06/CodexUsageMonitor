const {spawn} = require('node:child_process');
const fs = require('node:fs');
const assert = require('node:assert/strict');
const profile = fs.mkdtempSync('/tmp/codex-analytics-browser-');
const chrome = spawn('/usr/bin/google-chrome', ['--headless','--no-sandbox','--no-proxy-server','--disable-background-networking','--disable-gpu','--disable-dev-shm-usage','--no-first-run','--remote-debugging-pipe',`--user-data-dir=${profile}`,'about:blank'], {stdio:['ignore','ignore','pipe','pipe','pipe']});
let buffer='', id=0;
const pending=new Map(), errors=[];
chrome.stderr.on('data',()=>{});
chrome.stdio[4].on('data', chunk=>{
  buffer+=chunk.toString(); let end;
  while((end=buffer.indexOf('\0'))>=0) {
    const message=JSON.parse(buffer.slice(0,end)); buffer=buffer.slice(end+1);
    if(message.id && pending.has(message.id)) { const {resolve,reject,timer}=pending.get(message.id);clearTimeout(timer);pending.delete(message.id);message.error?reject(new Error(JSON.stringify(message.error))):resolve(message.result); }
    if(message.method==='Runtime.exceptionThrown') errors.push(message.params.exceptionDetails);
  }
});
function send(method,params={},sessionId) { return new Promise((resolve,reject)=>{const requestId=++id;const timer=setTimeout(()=>{pending.delete(requestId);reject(new Error('Timeout: '+method));},45000);pending.set(requestId,{resolve,reject,timer});chrome.stdio[3].write(JSON.stringify({id:requestId,method,params,...(sessionId?{sessionId}:{})})+'\0');}); }
(async()=>{
  try {
    const {targetId}=await send('Target.createTarget',{url:'about:blank'});
    const {sessionId}=await send('Target.attachToTarget',{targetId,flatten:true});
    const call=(method,params)=>send(method,params,sessionId);
    await call('Page.enable');await call('Runtime.enable');await call('Network.enable');
    await call('Emulation.setDeviceMetricsOverride',{width:1440,height:1050,deviceScaleFactor:1,mobile:false});
    await call('Network.setExtraHTTPHeaders',{headers:{Authorization:'Basic '+Buffer.from('admin:test-password').toString('base64')}});
    await call('Page.navigate',{url:'http://127.0.0.1:18766/'});
    const evaluate=async expression=>{const r=await call('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true});if(r.exceptionDetails)throw Error(JSON.stringify(r.exceptionDetails));return r.result.value;};
    const wait=condition=>evaluate(`(async()=>{for(let i=0;i<200;i++){if(${condition})return true;await new Promise(r=>setTimeout(r,100));}throw Error('Timed out: '+${JSON.stringify(condition)})})()`);
    await wait("document.getElementById('connectionText')?.textContent==='모니터 서버 연결됨'");
    const initial=await evaluate("({account:state.accountKey,sessionTokens:state.data.summary.session_tokens,options:document.getElementById('monitorAccountSelect').options.length})");
    assert.deepEqual(initial,{account:'aaaaaaaaaaaa',sessionTokens:100,options:2});
    await evaluate("document.getElementById('monitorAccountSelect').value='bbbbbbbbbbbb';document.getElementById('monitorAccountSelect').dispatchEvent(new Event('change'))");
    await wait("state.accountKey==='bbbbbbbbbbbb' && !state.refreshing");
    assert.equal(await evaluate('state.data.summary.session_tokens'),700);
    assert.equal(await evaluate('state.data.summary.lifetime_tokens'),9000);
    // Reordered requests must leave the most recent account selected.
    await evaluate(`window.originalFetch=window.fetch;window.fetch=async(...args)=>{if(String(args[0]).includes('account_key=aaaaaaaaaaaa'))await new Promise(r=>setTimeout(r,300));return window.originalFetch(...args)};const select=document.getElementById('monitorAccountSelect');select.value='aaaaaaaaaaaa';select.dispatchEvent(new Event('change'));select.value='bbbbbbbbbbbb';select.dispatchEvent(new Event('change'))`);
    await wait('!state.refreshing');await evaluate('new Promise(r=>setTimeout(r,450))');
    assert.equal(await evaluate('state.data.selected_account_key'),'bbbbbbbbbbbb');
    await evaluate('window.fetch=window.originalFetch');
    await evaluate("document.getElementById('manageAccounts').click();document.getElementById('discoveredAccountSelect').value='cccccccccccc';document.getElementById('discoveredAccountSelect').dispatchEvent(new Event('change'));document.getElementById('registerAccountLabel').value='Account C';document.getElementById('registerAccountForm').requestSubmit()");
    await wait("state.data.accounts.registered.length===3 && !state.refreshing");
    await evaluate(`document.querySelector('[data-account-key="bbbbbbbbbbbb"] [data-action="toggle"]').click()`);
    await wait("state.data.accounts.registered.find(x=>x.account_key==='bbbbbbbbbbbb').enabled===false && !state.refreshing");
    assert.equal(await evaluate('state.data.summary.session_tokens'),700);
    assert.match(await evaluate("document.getElementById('monitorAccountStatus').textContent"),/수집 정지/);
    await evaluate(`document.querySelector('[data-account-key="cccccccccccc"] [data-action="default"]').click()`);
    await wait("state.data.accounts.default_account_key==='cccccccccccc' && !state.refreshing");
    assert.equal(await evaluate('state.data.selected_account_key'),'bbbbbbbbbbbb');
    assert.equal(await evaluate("fetch('/api/dashboard').then(r=>r.json()).then(d=>d.selected_account_key)"),'cccccccccccc');
    await call('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
    assert.equal(await evaluate('document.documentElement.scrollWidth<=innerWidth+1'),true);
    await call('Page.reload');await wait("document.getElementById('connectionText')?.textContent==='모니터 서버 연결됨'");
    assert.equal(await evaluate('state.accountKey'),'cccccccccccc');
    assert.equal(await evaluate('state.data.summary.session_tokens'),0);
    assert.deepEqual(errors,[]);
    console.log('PASS: account switching, stale response protection, registration, pause/history, persistent default, mobile, reload; JS errors = 0');
  } finally {await send('Browser.close').catch(()=>{});chrome.kill();chrome.stdio[2].destroy();chrome.stdio[3].destroy();chrome.stdio[4].destroy();}
})().catch(error=>{console.error(error);process.exitCode=1;});
