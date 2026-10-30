// Apps Script, привязанный к одной Google Таблице.
// SPREADSHEET_ID — часть URL между /d/ и /edit. API_KEY должен совпадать с GOOGLE_SCRIPT_KEY в .env.
const SPREADSHEET_ID = 'ВСТАВЬ_ID_СВОЕЙ_ТАБЛИЦЫ';
const API_KEY = 'ВСТАВЬ_ДЛИННЫЙ_СЛУЧАЙНЫЙ_СЕКРЕТ';
const META_SHEET = '_Задания';
const ROSTER_STATUS_HEADER = 'Состав';
const NAME_HEADER = 'ФИО ученика';

function doPost(e) {
  try {
    const data = JSON.parse((e.postData && e.postData.contents) || '{}');
    if (data.api_key !== API_KEY) return json({ok:false, error:'Unauthorized'});
    let result;
    switch (data.action) {
      case 'create_task': result = createTask(data); break;
      case 'update_submission': result = updateSubmission(data); break;
      case 'sync_roster': result = syncRoster(data.class_name, data.students || []); break;
      case 'close_task': result = closeTask(data); break;
      default: throw new Error('Неизвестное действие: ' + data.action);
    }
    return json({ok:true, result:result});
  } catch (err) {
    return json({ok:false, error:String(err && err.message ? err.message : err)});
  }
}

function cleanSheetName(name) {
  let s = String(name || 'Группа').replace(/[\\\/?*:\[\]]/g, '_').trim().substring(0, 90);
  return s || 'Группа';
}
function getSpreadsheet() { return SpreadsheetApp.openById(SPREADSHEET_ID); }
function getGroupSheet(className) {
  const ss = getSpreadsheet();
  const name = cleanSheetName(className);
  let sheet = ss.getSheetByName(name);
  if (!sheet) {
    sheet = ss.insertSheet(name);
    sheet.getRange(1,1,1,3).setValues([[NAME_HEADER, ROSTER_STATUS_HEADER, 'Заметки']]);
    sheet.setFrozenRows(1);
    sheet.getRange(1,1,1,3).setFontWeight('bold');
  }
  ensureMetaSheet();
  return sheet;
}
function ensureMetaSheet() {
  const ss = getSpreadsheet();
  let sh = ss.getSheetByName(META_SHEET);
  if (!sh) {
    sh = ss.insertSheet(META_SHEET);
    sh.getRange(1,1,1,6).setValues([['ID задания','Класс','Название','Условие','Срок сдачи','Статус']]);
    sh.setFrozenRows(1); sh.getRange(1,1,1,6).setFontWeight('bold');
    sh.hideSheet();
  }
  return sh;
}
function taskMetaRow(taskId) {
  const sh = ensureMetaSheet();
  const last = sh.getLastRow();
  if (last < 2) return 0;
  const ids = sh.getRange(2,1,last-1,1).getDisplayValues();
  for (let i=0;i<ids.length;i++) if (String(ids[i][0]) === String(taskId)) return i+2;
  return 0;
}
function taskColumn(sheet, taskId) {
  const lastCol = sheet.getLastColumn();
  if (lastCol < 4) return 0;
  const headers = sheet.getRange(1,4,1,lastCol-3).getDisplayValues()[0];
  const marker = '[ID:' + String(taskId) + ']';
  for (let i=0;i<headers.length;i++) if (String(headers[i]).indexOf(marker) >= 0) return i+4;
  return 0;
}
function syncRoster(className, students) {
  const sheet = getGroupSheet(className);
  const current = Array.from(new Set((students || []).map(s => String(s.full_name || '').trim()).filter(Boolean)));
  const lastRow = sheet.getLastRow();
  const existing = new Map();
  if (lastRow >= 2) {
    const names = sheet.getRange(2,1,lastRow-1,1).getDisplayValues();
    names.forEach((r,i) => { const n=String(r[0]||'').trim(); if(n) existing.set(n.toLocaleLowerCase('ru-RU'),i+2); });
  }
  // Не удаляем выбывших учеников физически: сохраняем историю старых заданий.
  if (lastRow >= 2) {
    const names = sheet.getRange(2,1,lastRow-1,1).getDisplayValues();
    names.forEach((r,i) => {
      const n=String(r[0]||'').trim(); if(!n) return;
      sheet.getRange(i+2,2).setValue(current.some(x=>x.toLocaleLowerCase('ru-RU')===n.toLocaleLowerCase('ru-RU')) ? 'В списке' : 'Выбыл');
    });
  }
  let added=0;
  current.forEach(name => {
    const key=name.toLocaleLowerCase('ru-RU');
    if (existing.has(key)) {
      sheet.getRange(existing.get(key),2).setValue('В списке');
    } else {
      const row = Math.max(2, sheet.getLastRow()+1);
      sheet.getRange(row,1,1,sheet.getLastColumn()).clearContent();
      sheet.getRange(row,1,1,2).setValues([[name,'В списке']]);
      added++;
    }
  });
  sheet.setFrozenRows(1); sheet.autoResizeColumns(1, sheet.getLastColumn());
  return {sheet_name:sheet.getName(), added:added, current_count:current.length, spreadsheet_url:getSpreadsheet().getUrl()};
}
function createTask(data) {
  const className = String(data.class_name || '').trim();
  const taskId = String(data.task_id || '').trim();
  if (!className || !taskId) throw new Error('Не указан класс или ID задания');
  const rosterResult = syncRoster(className, data.students || []);
  const sheet = getGroupSheet(className);
  if (taskColumn(sheet, taskId)) throw new Error('Столбец задания #' + taskId + ' уже существует');
  const col = Math.max(4, sheet.getLastColumn()+1);
  const title = String(data.title || 'Задание');
  const closed = 'ОТКРЫТО';
  const header = 'Задание #' + taskId + ' — ' + title + ' — срок ' + String(data.due_at || '') + ' [ID:' + taskId + ']';
  sheet.getRange(1,col).setValue(header);
  const lastRow = sheet.getLastRow();
  if (lastRow >= 2) {
    const rosterFlags = sheet.getRange(2,2,lastRow-1,1).getDisplayValues();
    rosterFlags.forEach((r,i) => sheet.getRange(i+2,col).setValue(String(r[0]) === 'В списке' ? 'Не сдано' : ''));
  }
  const meta = ensureMetaSheet();
  meta.appendRow([taskId,className,title,String(data.instructions||''),String(data.due_at||''),closed]);
  sheet.getRange(1,col,Math.max(1,sheet.getLastRow()),1).setWrap(true);
  sheet.getRange(1,1,1,sheet.getLastColumn()).setFontWeight('bold');
  sheet.autoResizeColumns(1,sheet.getLastColumn());
  return {sheet_name:sheet.getName(), sheet_id:sheet.getSheetId(), spreadsheet_url:getSpreadsheet().getUrl(), roster_added:rosterResult.added};
}
function updateSubmission(data) {
  const sheet = getGroupSheet(data.class_name);
  const col = taskColumn(sheet, data.task_id);
  if (!col) throw new Error('В листе группы не найдено задание #' + data.task_id);
  const target = String(data.student_name||'').trim().toLocaleLowerCase('ru-RU');
  const lastRow = sheet.getLastRow();
  if (lastRow < 2) throw new Error('В листе нет учеников');
  const names = sheet.getRange(2,1,lastRow-1,1).getDisplayValues();
  for (let i=0;i<names.length;i++) {
    if (String(names[i][0]||'').trim().toLocaleLowerCase('ru-RU') === target) {
      sheet.getRange(i+2,col).setValue(String(data.status||'Сдано') + ' — ' + String(data.submitted_at||''));
      const metaRow = taskMetaRow(data.task_id);
      if (metaRow) ensureMetaSheet().getRange(metaRow,6).setValue('Открыто');
      return {updated:true,row:i+2,column:col};
    }
  }
  throw new Error('Ученик не найден: ' + data.student_name);
}
function closeTask(data) {
  const sheet = getGroupSheet(data.class_name);
  const col = taskColumn(sheet, data.task_id);
  if (!col) throw new Error('В листе группы не найдено задание #' + data.task_id);
  const header = String(sheet.getRange(1,col).getDisplayValue());
  sheet.getRange(1,col).setValue(header.replace(' [ЗАКРЫТО]','') + ' [ЗАКРЫТО]');
  const row = taskMetaRow(data.task_id);
  if (row) ensureMetaSheet().getRange(row,6).setValue('Закрыто');
  return {closed:true,sheet_name:sheet.getName()};
}
function json(obj) { return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON); }
