// Return React-ready text/link segments; never interpret email content as HTML.
export function safeHref(value) {
 if (/[\s\u0000-\u001f\u007f]/.test(value)) return null;
 try {
  const url=new URL(value);
  if ((url.protocol==='https:'||url.protocol==='http:')&&url.hostname&&!url.username&&!url.password) return url.href;
  if (url.protocol==='mailto:'&&url.pathname) return url.href;
 } catch {}
 return null;
}
export function messageLinks(body) {
 const result=[];
 const pattern=/\[([^\]\n]+)\]\(((?:https?:\/\/|mailto:)[^\s<>]+)\)|(?:https?:\/\/|mailto:)[^\s<>"\[\]]+/gi;
 let offset=0;
 for(const match of body.matchAll(pattern)) {
  let value=match[2]||match[0],suffix='';
  if(!match[2]) {
   // Keep prose punctuation and unmatched closing brackets outside the anchor.
   while(/[.,;:!?]$/.test(value)||(/[)}]$/.test(value)&&([...value].filter(c=>c===value.at(-1)).length>[...value].filter(c=>c===(value.at(-1)===')'?'(':'{')).length))) {
    suffix=value.at(-1)+suffix;value=value.slice(0,-1);
   }
  }
  const href=safeHref(value);
  if(!href)continue;
  if(match.index>offset)result.push({text:body.slice(offset,match.index)});
  result.push({text:match[1]||value,href});
  if(suffix)result.push({text:suffix});
  offset=match.index+match[0].length;
 }
 if(offset<body.length)result.push({text:body.slice(offset)});
 return result;
}
