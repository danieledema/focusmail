import React from 'react';
import {messageLinks} from './messageLinks.mjs';
export default function MessageBody({body}) {
 return <div className="email-body">{messageLinks(body).map((part,index)=>part.href?<a key={index} href={part.href} rel="noopener noreferrer" referrerPolicy="no-referrer" onClick={event=>{
  if(window.__TAURI_INTERNALS__){event.preventDefault();window.location.assign(part.href)}
 }}>{part.text}</a>:<React.Fragment key={index}>{part.text}</React.Fragment>)}</div>;
}
