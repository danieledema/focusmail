import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readAIStream,partialAnswer} from '../src/components/ai-stream.mjs';

test('AI events survive arbitrary UTF-8 chunk boundaries',async()=>{
 const text='{"type":"delta","text":"caffè ☕"}\n{"type":"result","content":"Done","draft":null}\n';
 const bytes=new TextEncoder().encode(text),events=[];
 const response=new Response(new ReadableStream({start(c){for(const byte of bytes)c.enqueue(Uint8Array.of(byte));c.close()}}));
 await readAIStream(response,event=>events.push(event));
 assert.equal(events[0].text,'caffè ☕');assert.equal(events[1].content,'Done');
});
test('a dropped stream is not mistaken for completion',async()=>{
 await assert.rejects(readAIStream(new Response('{"type":"heartbeat"}\n'),()=>{}),/closed before/);
 await assert.rejects(readAIStream(new Response('{"type":"error","message":"Connection failed"}\n'),()=>{}),/Connection failed/);
});
test('partial answers decode content while keeping unfinished drafts hidden',()=>{
 assert.equal(partialAnswer('{"content":"Hello\\nworld","draft":"do not display'), 'Hello\nworld');
 assert.equal(partialAnswer('{"content":"Hello\\u00'), 'Hello');
 assert.equal(partialAnswer('{"draft":"do not display'), '');
 assert.equal(partialAnswer('A plain reply'), 'A plain reply');
});
