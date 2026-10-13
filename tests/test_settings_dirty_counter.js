const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '../templates/settings.html'), 'utf8');
const start = html.indexOf('let isFormDirty = false;');
const end = html.indexOf('// Initialize dirty check', start);
assert.ok(start >= 0 && end > start);
const checkbox = { name: 'ABS_ENABLED', type: 'checkbox', checked: false };
const text = { name: 'WHISPER_LANGUAGE', type: 'text', value: '' };
const select = { name: 'TRANSCRIPTION_PROVIDER', type: 'select-one', value: 'local' };
const picker = { id: 'shelf_picker', type: 'select-one', value: '' };
const hidden = { name: 'csrf_token', type: 'hidden', value: 'token' };
const fields = [checkbox, text, select, picker, hidden];
for (const field of fields) {
    field.handlers = {};
    field.addEventListener = (name, handler) => { field.handlers[name] = handler; };
}
const note = { style: {}, textContent: '' };
const windowHandlers = {};
const form = { addEventListener() {} };
const context = vm.createContext({
    document: {
        getElementById: () => note,
        querySelectorAll: selector => selector === 'form' ? [form] : fields,
    },
    window: { addEventListener: (name, handler) => { windowHandlers[name] = handler; } },
});
vm.runInContext(html.slice(start, end), context);
context.setupDirtyCheck();
const warning = () => {
    const event = {};
    windowHandlers.beforeunload(event);
    return event.returnValue;
};
assert.equal(warning(), undefined);
checkbox.checked = true;
checkbox.handlers.change();
assert.equal(note.textContent, '1 unsaved change');
assert.ok(warning());
text.value = 'ru';
text.handlers.input();
assert.equal(note.textContent, '2 unsaved changes');
checkbox.checked = false;
checkbox.handlers.change();
assert.equal(note.textContent, '1 unsaved change');
assert.ok(warning());
text.value = '';
text.handlers.input();
assert.equal(note.style.display, 'none');
assert.equal(warning(), undefined);
select.value = 'deepgram';
select.handlers.change();
select.value = 'local';
select.handlers.change();
assert.equal(warning(), undefined);
assert.equal(picker.handlers.change, undefined);
assert.equal(hidden.handlers.input, undefined);

// Shelf pickers update the actual saved input without a native input event.
const pickerStart = html.indexOf('function applyShelfPickerSelection(');
const pickerEnd = html.indexOf('function checkBookloreShelves(', pickerStart);
context.document.querySelector = () => text;
const shelfSelect = { value: 'Want to read' };
context.document.getElementById = id => id === 'dirty_note' ? note : shelfSelect;
vm.runInContext(html.slice(pickerStart, pickerEnd), context);
context.applyShelfPickerSelection(text.name, 'shelf_picker', true);
assert.equal(note.textContent, '1 unsaved change');
text.value = '';
text.handlers.change();
assert.equal(warning(), undefined);
