function fleetDurableBuffer(Base) {
  return class extends Base {
    constructor(onMutate) {
      super(onMutate);
      const fs=require('node:fs'),path=require('node:path');
      const directory=path.join(require('node:os').homedir(),'.local/state/fleet/memory/native');
      fs.mkdirSync(directory,{recursive:true,mode:0o700});
      const directoryStat=fs.lstatSync(directory);
      if(!directoryStat.isDirectory() || directoryStat.uid!==process.getuid() || directoryStat.mode&0o077)throw Error('Unsafe memory queue directory');
      const file=path.join(directory,'pending.db');
      if(fs.existsSync(file)) {
        const info=fs.lstatSync(file);
        if(!info.isFile() || info.nlink!==1 || info.uid!==process.getuid())throw Error('Unsafe memory queue file');
      }
      this.fleetDb=new (require('bun:sqlite').Database)(file,{create:true});
      fs.chmodSync(file,0o600);
      this.fleetDb.exec('PRAGMA busy_timeout=2000; PRAGMA synchronous=FULL; CREATE TABLE IF NOT EXISTS pending(id INTEGER PRIMARY KEY,session INTEGER NOT NULL,payload TEXT NOT NULL,enqueued INTEGER NOT NULL)');
      for(const row of this.fleetDb.query('SELECT * FROM pending ORDER BY id').all()) {
        const message=JSON.parse(row.payload);
        this.getList(row.session).push({id:row.id,message,claimed:false,enqueuedAt:row.enqueued});
        if(message.toolUseId)this.getSeen(row.session).add(message.toolUseId);
        this.nextId=Math.max(this.nextId,row.id+1);
      }
    }
    enqueue(session,message) {
      const payload=jo(JSON.stringify(message));
      const filtered=JSON.parse(payload),time=Date.now();
      if(filtered.toolUseId && this.getSeen(session).has(filtered.toolUseId))return 0;
      this.fleetDb.query('INSERT INTO pending VALUES(?,?,?,?)').run(this.nextId,session,payload,time);
      const id=super.enqueue(session,filtered);
      this.getList(session).at(-1).enqueuedAt=time;
      return id;
    }
    confirm(id) {
      this.fleetDb.query('DELETE FROM pending WHERE id=?').run(id);
      return super.confirm(id);
    }
    clear(session) {
      return this.getPendingCount(session)?0:super.clear(session);
    }
    dispose(session) {
      if(!this.getPendingCount(session))super.dispose(session);
    }
  };
}

function fleetMemoryCanRun(manager,session,source) {
  const recovery=source==='durable-recovery' || source==='overflow-recycle';
  if(!recovery)session.fleetActivityUntil=Date.now()+60000;
  const items=manager.getMessageBuffer().buffers.get(session.sessionDbId);
  if(!items?.length || !manager.getMessageBuffer().getPendingCount(session.sessionDbId))return false;
  const latest=items.at(-1).id;
  if(recovery && (!(session.fleetActivityUntil>Date.now()) || session.fleetRecoveryThrough===latest))return false;
  session.fleetRecoveryThrough=latest;
  return true;
}

function fleetRecoverPending(worker,routes) {
  if(!worker.initializationCompleteFlag || worker.isShuttingDown)return;
  const manager=worker.sessionManager;
  let active=[...manager.sessions.values()].filter(session=>session.generatorPromise && manager.getMessageBuffer().getPendingCount(session.sessionDbId)).length;
  for(const [id,items] of manager.getMessageBuffer().buffers) {
    if(active>=2)break;
    if(!items.length)continue;
    const session=manager.getSession(id);
    if(!session || !(session.fleetActivityUntil>Date.now()) || session.fleetRecoveryThrough===items.at(-1).id || session.generatorPromise || session.fleetRetryAt>Date.now() || session.fleetRecoveryAt>Date.now())continue;
    session.fleetRecoveryAt=Date.now()+60000;
    active++;
    routes.ensureGeneratorRunning(id,'durable-recovery').catch(error=>v.error('SESSION','Memory queue recovery failed',{sessionId:id},error));
  }
}

let fleetClaudeFullUntil=0;
function fleetMemoryConfig() {
  const base=of('ANTHROPIC_BASE_URL'),key=of('ANTHROPIC_AUTH_TOKEN');
  if(!base || !key)throw Error('Fleet memory gateway is not configured');
  const url=new URL('/v1/chat/completions',base);
  if(url.protocol!=='http:' || url.hostname!=='127.0.0.1')throw Error('Fleet memory requires its loopback gateway');
  const configured=we.loadFromFile(rt).CLAUDE_MEM_MODEL;
  return {apiKey:key,apiUrl:url.href,model:configured==='haiku'?'claude-haiku-4-5-20251001':configured,siteUrl:'',appName:'Fleet memory'};
}
async function fleetMemoryFetch(url,key,model,messages,site,app,prior,signal) {
  const slot=await L8(2,signal);
  try {
    const send=async selected=>{
      const response=await fetch(url,{
        method:'POST',signal,
        headers:{Authorization:`Bearer ${key}`,...Pme(site,app),'Content-Type':'application/json',...(prior?{'x-claude-mem-prior-request-id':prior}:{})},
        body:JSON.stringify({model:selected,messages,temperature:.3,max_tokens:4096,...(selected==='gpt-6.1-terra'?{reasoning_effort:'high'}:{})})
      });
      return new Response(await response.arrayBuffer(),{status:response.status,headers:response.headers});
    };
    if(Date.now()<fleetClaudeFullUntil)return await send('gpt-6.1-terra');
    const response=await send(model);
    if([402,429,503].includes(response.status)) {
      const body=await response.clone().text();
      if(response.status!==503 || /fleet_credit_(blocked|usage_unavailable)/.test(body)) {
        fleetClaudeFullUntil=Date.now()+10*60000;
        v.warn('SDK','Claude memory allowance unavailable; using gpt-6.1-terra with high reasoning');
        return await send('gpt-6.1-terra');
      }
    }
    return response;
  } finally {
    slot.release();
  }
}
