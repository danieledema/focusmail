import test from 'node:test';
import assert from 'node:assert/strict';
import {shortcutAction,SHORTCUTS} from '../src/components/shortcuts.mjs';

test('lowercase shortcuts use a for archive and d for delete',()=>{
 for(const [key,action] of [['r','reply'],['s','snooze'],['a','archive'],['d','delete'],['?','help']])assert.equal(shortcutAction({key}),action);
 for(const key of ['A','D','R','S','e','Delete'])assert.equal(shortcutAction({key}),null);
 assert.equal(SHORTCUTS[2][0],'a');assert.equal(SHORTCUTS[3][0],'d');
});
test('editing and system shortcuts never archive or delete',()=>{
 for(const key of ['a','d']) {
  for(const modifier of ['ctrlKey','metaKey','altKey','shiftKey','defaultPrevented'])assert.equal(shortcutAction({key,[modifier]:true}),null);
  assert.equal(shortcutAction({key,target:{closest:()=>true}}),null);
 }
 assert.equal(shortcutAction({key:'?',shiftKey:true}),'help');
});
