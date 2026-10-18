export const SHORTCUTS = [
 ['r','Open a reply','reply'],
 ['s','Choose a snooze time','snooze'],
 ['a','Archive the conversation','archive'],
 ['d','Move to Trash (15 seconds to undo)','delete'],
 ['?','Open help','help'],
];
export function shortcutAction(event) {
 if(event.defaultPrevented||event.ctrlKey||event.metaKey||event.altKey||event.shiftKey&&event.key!=='?')return null;
 if(event.target?.closest?.('input,textarea,select,[contenteditable="true"]'))return null;
 return SHORTCUTS.find(([key])=>key===event.key)?.[2]||null;
}
