// Decode newline-delimited events across arbitrary UTF-8/network chunk boundaries.
export async function readAIStream(response,onEvent) {
 if(!response.ok){const data=await response.json().catch(()=>({}));throw new Error(typeof data.detail==='string'?data.detail:'Could not start the AI request. Check your connection and try again.')}
 if(!response.body)throw new Error('Streaming is unavailable in this browser.');
 const reader=response.body.getReader(),decoder=new TextDecoder();let buffer='',complete=false;
 const accept=line=>{if(!line.trim())return;const event=JSON.parse(line);if(event.type==='error')throw new Error(event.message);onEvent(event);if(event.type==='result')complete=true};
 try{while(true){const {value,done}=await reader.read();buffer+=decoder.decode(value,{stream:!done});let end;while((end=buffer.indexOf('\n'))!==-1){accept(buffer.slice(0,end));buffer=buffer.slice(end+1)}if(done)break}if(buffer.trim())accept(buffer);if(!complete)throw new Error('The connection to FocusMail closed before the reply finished. Please try again.')}
 finally{await reader.cancel().catch(()=>{});reader.releaseLock()}
}

export function partialAnswer(raw){
 const text=raw.trimStart().replace(/^```(?:json)?\s*/,'');
 if(!text.startsWith('{'))return text;
 const match=text.match(/"content"\s*:\s*"((?:[^"\\]|\\.)*)/s);
 if(!match)return '';
 // A final escape or Unicode sequence may span multiple tokens.
 for(let cut=0;cut<=6&&cut<=match[1].length;cut++){
  try{return JSON.parse('"'+match[1].slice(0,match[1].length-cut)+'"')}catch{}
 }
 return '';
}

export const AI_STAGES={
 connecting:'Connecting to your AI server…',
 waiting:'Request sent. Waiting for the server’s reply…',
 responding:'Receiving your answer…'
};
