import test from 'node:test';
import assert from 'node:assert/strict';
import {messageLinks,safeHref} from '../src/components/messageLinks.mjs';

test('paper titles link to their preserved destinations',()=>{
 assert.deepEqual(messageLinks('Read [The paper](https://example.com/paper?a=1&b=2).'),[
  {text:'Read '},{text:'The paper',href:'https://example.com/paper?a=1&b=2'},{text:'.'}
 ]);
});
test('plain URLs preserve balanced parentheses and leave punctuation outside',()=>{
 const body='See (https://example.com/a(b)). Then https://example.org/x, thanks.';
 const parts=messageLinks(body);
 assert.deepEqual(parts.filter(p=>p.href).map(p=>p.href),['https://example.com/a(b)','https://example.org/x']);
 assert.equal(parts.map(p=>p.text).join(''),body);
});
test('email links work while dangerous schemes, credentials and controls are rejected',()=>{
 assert.equal(safeHref('mailto:author@example.com'),'mailto:author@example.com');
 for(const value of ['javascript:alert(1)','data:text/html,evil','file:///tmp/data','https://user:secret@example.com','https://example.com/\nscript'])assert.equal(safeHref(value),null);
 assert.deepEqual(messageLinks('[Bad](javascript:alert(1))'),[{text:'[Bad](javascript:alert(1))'}]);
});
test('HTML and script-like text remain literal text segments',()=>{
 assert.deepEqual(messageLinks('<img onerror=alert(1)>'),[{text:'<img onerror=alert(1)>'}]);
});
