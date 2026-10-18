import React, {forwardRef,useEffect,useImperativeHandle,useState} from 'react';
import {ArrowUpRight,Check,ChevronDown,ExternalLink,Loader2,Settings2,ShieldCheck,X} from 'lucide-react';

const names = {google:'Google',microsoft:'Microsoft'};
function ProviderMark({provider}) {
 return provider==='google'?<span className="google-mark" aria-hidden="true">G</span>:<span className="microsoft-mark" aria-hidden="true"><i/><i/><i/><i/></span>;
}
function external(event,url) {
 if(window.__TAURI_INTERNALS__){event.preventDefault();window.location.assign(url)}
}

export default forwardRef(function OAuthConnections({api,demo,refresh,notify},ref){
 const [providers,setProviders]=useState([]),[setup,setSetup]=useState(null),[config,setConfig]=useState({client_id:'',tenant:'common',client_secret:''}),[saving,setSaving]=useState(false),[attempt,setAttempt]=useState(null),[message,setMessage]=useState(''),[flowError,setFlowError]=useState(false);
 const load=async()=>{try{setProviders(await api('/oauth/providers'))}catch(e){setMessage(e.message);setFlowError(true)}};
 useEffect(()=>{load()},[]);
 useEffect(()=>{
  if(!attempt)return;
  let closed=false;
  const poll=async()=>{
   try{
    const result=await api('/oauth/attempts/'+attempt.attempt_id);
    if(closed)return;
    setMessage(result.message);
    if(result.status==='success'){
     setAttempt(null);setFlowError(false);await refresh();notify(`${names[result.provider]} account connected. Press Sync inbox when you’re ready.`);
    }else if(['error','cancelled'].includes(result.status)){setAttempt(null);setFlowError(result.status==='error')}
   }catch(e){if(!closed){setMessage(e.message);setFlowError(true);setAttempt(null)}}
  };
  const timer=setInterval(poll,1500);poll();
  return()=>{closed=true;clearInterval(timer)};
 },[attempt?.attempt_id]);
 const configure=provider=>{
  const item=providers.find(p=>p.id===provider);
  setSetup(provider);setConfig({client_id:item?.client_id||'',tenant:item?.tenant||'common',client_secret:''});setMessage('');setFlowError(false);
 };
 const connect=async(provider,accountId=null)=>{
  if(demo)return;
  if(attempt){setMessage('Finish or cancel the sign-in already in progress.');return}
  const item=providers.find(p=>p.id===provider);
  if(!item?.configured){configure(provider);return}
  const native=!!window.__TAURI_INTERNALS__;
  // Open during the click gesture; OAuth itself must run in a real browser.
  const popup=native?null:window.open('about:blank',`focusmail-${provider}-signin`);
  if(popup){popup.document.title='FocusMail sign-in';popup.document.body.textContent='Preparing secure sign-in…'}
  setSaving(true);setMessage('Preparing sign-in…');setFlowError(false);
  try{
   const result=await api('/oauth/'+provider+'/start',{method:'POST',body:JSON.stringify({account_id:accountId})});
   setAttempt({...result,provider});setSetup(null);setMessage('Continue signing in in your browser. This window will update when you’re connected.');
   if(native)window.location.assign(result.authorization_url);
   else if(popup)popup.location.replace(result.authorization_url);
   else setMessage('Your browser blocked the sign-in tab. Use Open sign-in to continue.');
  }catch(e){if(popup)popup.close();setMessage(e.message);setFlowError(true)}finally{setSaving(false)}
 };
 useImperativeHandle(ref,()=>({connect}));
 const save=async e=>{
  e.preventDefault();setSaving(true);setFlowError(false);
  const payload={client_id:config.client_id.trim(),tenant:config.tenant};
  if(setup==='google'&&config.client_secret)payload.client_secret=config.client_secret;
  try{await api('/oauth/providers/'+setup,{method:'PUT',body:JSON.stringify(payload)});await load();setConfig(c=>({...c,client_secret:''}));setSetup(null);setMessage(`${names[setup]} setup saved. You can connect your account now.`)}catch(e){setMessage(e.message);setFlowError(true)}finally{setSaving(false)}
 };
 const cancel=async()=>{
  try{await api('/oauth/attempts/'+attempt.attempt_id,{method:'DELETE'});setAttempt(null);setMessage('Sign-in cancelled.');setFlowError(false)}catch(e){setMessage(e.message);setFlowError(true)}
 };
 const reopen=()=>{
  if(window.__TAURI_INTERNALS__)window.location.assign(attempt.authorization_url);
  else window.open(attempt.authorization_url,'_blank','noopener,noreferrer');
 };
 return <section className="oauth-connections" aria-label="Connect an email provider">
  <div className="oauth-intro"><ShieldCheck size={18}/><div><h3>Connect securely. Keep your password yours.</h3><p>Sign in with your provider, or use manual IMAP below.</p></div></div>
  <div className="provider-buttons">{['google','microsoft'].map(provider=>{
   const item=providers.find(p=>p.id===provider);
   return <div className="provider-choice" key={provider}><button type="button" className="provider-connect" disabled={demo||saving||!!attempt||!providers.length} onClick={()=>connect(provider)}><ProviderMark provider={provider}/><span>Connect {names[provider]}<small>{item?.configured?'Sign in with OAuth':'One-time app setup needed'}</small></span><ArrowUpRight size={15}/></button><button type="button" className="provider-setup" aria-label={`Set up ${names[provider]} OAuth`} disabled={demo||saving||!!attempt} onClick={()=>setup===provider?setSetup(null):configure(provider)}><Settings2 size={12}/> App setup</button></div>
  })}</div>
  {attempt&&<div className="oauth-waiting"><Loader2 size={17} className="spin"/><div><strong>Waiting for {names[attempt.provider]}</strong><p>You can finish sign-in in your browser. No mail is being fetched.</p><button type="button" onClick={reopen}>Open sign-in <ExternalLink size={12}/></button><button type="button" onClick={cancel}>Cancel</button></div></div>}
  {message&&<p className={`oauth-message ${flowError?'error':''}`} role={flowError?'alert':'status'}>{message}</p>}
  {setup&&<form className="oauth-setup-form" onSubmit={save}>
   <header><h4>{names[setup]} app setup</h4><button type="button" className="icon-button" aria-label="Close OAuth setup" onClick={()=>setSetup(null)}><X size={16}/></button></header>
   <p>This identifies FocusMail to {names[setup]}. Configure it once; each email account then signs in separately. It is not your email password.</p>
   {setup==='google'?<ol><li>Create a Google OAuth client of type <strong>Desktop app</strong>.</li><li>Configure the consent screen with the mail scope <code>https://mail.google.com/</code> and add yourself as a test user if the app is in testing.</li><li>Copy the client ID below. If Google supplies a client secret, add it too.</li></ol>:<ol><li>Register an application supporting <strong>organizational and personal Microsoft accounts</strong>.</li><li>Add a <strong>Mobile and desktop applications</strong> redirect: <code>http://localhost/oauth/callback</code>.</li><li>Add delegated Office 365 Exchange Online permissions <code>IMAP.AccessAsUser.All</code> and <code>SMTP.Send</code>. FocusMail requests offline access for persistent sign-in.</li></ol>}
   <a className="oauth-docs" href={providers.find(p=>p.id===setup)?.help_url} target="_blank" rel="noreferrer" onClick={e=>external(e,providers.find(p=>p.id===setup).help_url)}>Official provider instructions <ExternalLink size={12}/></a>
   <label>{names[setup]} client ID<input aria-label={`${names[setup]} client ID`} autoComplete="off" required value={config.client_id} onChange={e=>setConfig(c=>({...c,client_id:e.target.value}))} placeholder={setup==='google'?'…apps.googleusercontent.com':'Application (client) ID'}/></label>
   {setup==='google'?<label>Client secret, if supplied<input aria-label="Google OAuth client secret" type="password" autoComplete="new-password" value={config.client_secret} onChange={e=>setConfig(c=>({...c,client_secret:e.target.value}))} placeholder={providers.find(p=>p.id==='google')?.has_client_secret?'Saved locally — leave blank to keep':'Optional desktop client secret'}/></label>:<label>Microsoft account directory<select aria-label="Microsoft account directory" value={config.tenant} onChange={e=>setConfig(c=>({...c,tenant:e.target.value}))}><option value="common">Personal and work/school accounts</option><option value="consumers">Personal accounts only</option><option value="organizations">Work/school accounts only</option></select></label>}
   <p className="oauth-setup-note">Authorization requires your provider’s approval. Work/school policies may require administrator consent or block IMAP/SMTP. Saved secrets and tokens are encrypted locally.</p>
   <div className="form-actions"><button type="button" className="secondary" onClick={()=>setSetup(null)}>Cancel</button><button className="primary" disabled={saving}>{saving?<Loader2 size={14} className="spin"/>:<Check size={14}/>}Save app setup</button></div>
  </form>}
 </section>
});
